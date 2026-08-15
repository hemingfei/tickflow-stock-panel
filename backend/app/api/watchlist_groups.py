"""自选板块 API。"""
from __future__ import annotations

import logging
import math
import time
from datetime import date

import polars as pl
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.db_safe import is_valid_ext_ident, quote_ident
from app.services import watchlist_groups as watchlist_groups_service
from app.services import preferences

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/watchlist-groups", tags=["watchlist-groups"])


class CreateGroupRequest(BaseModel):
    name: str


class RenameGroupRequest(BaseModel):
    name: str


class ReorderGroupsRequest(BaseModel):
    group_ids: list[str]


class AddItemRequest(BaseModel):
    symbol: str
    note: str = ""


class AddItemsBatchRequest(BaseModel):
    symbols: list[str]
    note: str = ""


class ReorderItemsRequest(BaseModel):
    symbols: list[str]


class SetViewModeRequest(BaseModel):
    mode: str


class SetDisplayStyleRequest(BaseModel):
    style: str


class SetAvgPctModeRequest(BaseModel):
    mode: str


def _with_names(rows: list[dict], request: Request) -> list[dict]:
    if not rows:
        return rows
    try:
        name_map = request.app.state.repo.get_name_map([r.get("symbol") for r in rows])
        if not name_map:
            return rows
        return [{**row, "name": name_map.get(row.get("symbol"))} for row in rows]
    except Exception as e:  # noqa: BLE001
        logger.debug("attach watchlist names failed: %s", e)
        return rows


def _enrich_group_with_stats(group: dict, request: Request) -> dict:
    """为单个板块添加统计信息（包括平均涨跌幅）"""
    group_copy = group.copy()
    
    # 暂时将平均涨跌幅设为 None，避免复杂计算导致 500 错误
    # 后续可以在前端基于 enriched 数据计算
    group_copy["avg_change_pct"] = None
    group_copy["avg_change_pct_weighted"] = None
    
    return group_copy


@router.get("")
def list_all_groups(request: Request):
    try:
        groups = watchlist_groups_service.get_groups_with_stats()
        # 为每个板块添加平均涨跌幅
        enriched_groups = [_enrich_group_with_stats(g, request) for g in groups]
        return {"groups": enriched_groups}
    except Exception as e:
        logger.exception("Error listing groups")
        raise HTTPException(status_code=500, detail=f"获取板块列表失败: {str(e)}")


@router.post("")
def create_group(req: CreateGroupRequest, request: Request):
    try:
        if not req.name or not req.name.strip():
            raise HTTPException(400, "板块名称不能为空")
        groups = watchlist_groups_service.create_group(req.name)
        enriched_groups = [_enrich_group_with_stats(g, request) for g in groups]
        return {"groups": enriched_groups}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        logger.exception("Error creating group")
        raise HTTPException(500, f"创建板块失败: {str(e)}") from e


@router.patch("/{group_id}")
def rename_group(group_id: str, req: RenameGroupRequest, request: Request):
    try:
        groups = watchlist_groups_service.rename_group(group_id, req.name)
        enriched_groups = [_enrich_group_with_stats(g, request) for g in groups]
        return {"groups": enriched_groups}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/{group_id}")
def delete_group(group_id: str, request: Request):
    try:
        groups = watchlist_groups_service.delete_group(group_id)
        enriched_groups = [_enrich_group_with_stats(g, request) for g in groups]
        return {"groups": enriched_groups}
    except Exception as e:
        logger.exception("Error deleting group")
        raise HTTPException(500, f"删除板块失败: {str(e)}") from e


@router.post("/order")
def reorder_groups(req: ReorderGroupsRequest, request: Request):
    try:
        groups = watchlist_groups_service.reorder_groups(req.group_ids)
        enriched_groups = [_enrich_group_with_stats(g, request) for g in groups]
        return {"groups": enriched_groups}
    except Exception as e:
        logger.exception("Error reordering groups")
        raise HTTPException(500, f"排序板块失败: {str(e)}") from e


@router.get("/{group_id}/items")
def list_group_items(group_id: str, request: Request):
    try:
        items = watchlist_groups_service.list_group_items(group_id)
        return {"items": _with_names(items, request)}
    except Exception as e:
        logger.exception("Error listing group items")
        raise HTTPException(500, f"获取板块标的失败: {str(e)}") from e


@router.post("/{group_id}/items")
def add_item(group_id: str, req: AddItemRequest, request: Request):
    try:
        items = watchlist_groups_service.add_item(group_id, req.symbol, req.note)
        return {"items": _with_names(items, request)}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/{group_id}/items/batch")
def add_items_batch(group_id: str, req: AddItemsBatchRequest, request: Request):
    try:
        items, added = watchlist_groups_service.add_items_batch(group_id, req.symbols, req.note)
        return {"items": _with_names(items, request), "added": added}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/{group_id}/items/{symbol}")
def remove_item(group_id: str, symbol: str, request: Request):
    try:
        items = watchlist_groups_service.remove_item(group_id, symbol)
        return {"items": _with_names(items, request)}
    except Exception as e:
        logger.exception("Error removing item from group")
        raise HTTPException(500, f"删除标的失败: {str(e)}") from e


@router.post("/{group_id}/items/order")
def reorder_items(group_id: str, req: ReorderItemsRequest, request: Request):
    try:
        items = watchlist_groups_service.reorder_items(group_id, req.symbols)
        return {"items": _with_names(items, request)}
    except Exception as e:
        logger.exception("Error reordering group items")
        raise HTTPException(500, f"排序标的失败: {str(e)}") from e


# 自选页需要的列
_WATCHLIST_COLS = [
    "symbol", "close", "change_pct", "change_amount", "amount",
    "turnover_rate",
    "amplitude", "annual_vol_20d",
    "vol_ratio_5d",
    "ma5", "ma10", "ma20", "ma60",
    "vol_ma5", "vol_ma10",
    "high_60d", "low_60d",
    "rsi_6", "rsi_14", "rsi_24",
    "macd_dif", "macd_dea", "macd_hist",
    "kdj_k", "kdj_d", "kdj_j",
    "boll_upper", "boll_lower",
    "atr_14",
    "momentum_5d", "momentum_10d", "momentum_20d", "momentum_30d", "momentum_60d",
    "consecutive_limit_ups", "consecutive_limit_downs",
    "signal_limit_up", "signal_limit_down", "signal_volume_surge",
    "signal_ma_golden_5_20", "signal_macd_golden", "signal_n_day_high",
    "signal_boll_breakout_upper", "signal_ma20_breakout",
    "signal_ma_dead_5_20", "signal_macd_dead", "signal_n_day_low",
    "signal_boll_breakdown_lower", "signal_ma20_breakdown",
]


@router.get("/{group_id}/items/enriched")
def group_items_enriched(
    group_id: str,
    request: Request,
    ext_columns: str | None = Query(None, description="逗号分隔的 ext 列: config_id.field_name"),
):
    """自选板块 enriched 数据 — 直接从 enriched 最新日读取, 无即时计算。

    ext_columns 参数示例: "industry_rating.score,fund_flow.net_inflow"
    会动态 LEFT JOIN 对应的 ext_{config_id} DuckDB view。
    """
    t0 = time.perf_counter()

    repo = request.app.state.repo
    items = watchlist_groups_service.list_group_items(group_id)
    symbols = [r["symbol"] for r in items]
    if not symbols:
        return {"rows": [], "as_of": None, "elapsed_ms": 0}

    # 按资产拆分自选 symbol; ETF enriched 是独立缓存, 仅自选真的含 ETF 才去加载
    # (避免无 ETF 用户在缓存冷启动时触发 ETF 全量懒加载)
    etf_set = repo.get_etf_symbol_set()
    index_set = repo.get_index_symbol_set()
    etf_symbols = [s for s in symbols if s in etf_set]
    index_symbols = [s for s in symbols if s not in etf_set and s in index_set]
    stock_symbols = [s for s in symbols if s not in etf_set and s not in index_set]

    df_e, cache_date = repo.get_enriched_latest()

    # 以自选列表为主表 LEFT JOIN enriched, 保证自选的每一只都返回一行;
    # 不在 enriched 缓存里的标的 (新股/冷门股/新用户未同步) 指标为 null, 前端渲染为 "—".
    # 旧实现是 df_e.filter(is_in(stock_symbols)), 方向反了 (以 enriched 为主),
    # 会把不在缓存 universe 里的自选股静默丢弃.
    if stock_symbols:
        watchlist_df = pl.DataFrame({"symbol": stock_symbols})
        if df_e.is_empty():
            df = watchlist_df
        else:
            df = watchlist_df.join(df_e, on="symbol", how="left")
    else:
        df = pl.DataFrame()

    # ETF 行合并; 缺失列 (换手率/涨跌停信号等) 为 null
    etf_date = None
    if etf_symbols:
        df_etf_all, etf_date = repo.get_enriched_latest_asset("etf")
        etf_watchlist_df = pl.DataFrame({"symbol": etf_symbols})
        if not df_etf_all.is_empty():
            # ETF 同样以自选为主表 LEFT JOIN, 缺失标的指标为 null
            df_etf = etf_watchlist_df.join(df_etf_all, on="symbol", how="left")
        else:
            df_etf = etf_watchlist_df
        df = df_etf if df.is_empty() else pl.concat([df, df_etf], how="diagonal_relaxed")

    # 指数行合并 (镜像 ETF 分支); 缺失列 (换手率/涨跌停信号等) 为 null
    index_date = None
    if index_symbols:
        df_idx_all, index_date = repo.get_enriched_latest_asset("index")
        idx_watchlist_df = pl.DataFrame({"symbol": index_symbols})
        if not df_idx_all.is_empty():
            df_idx = idx_watchlist_df.join(df_idx_all, on="symbol", how="left")
        else:
            df_idx = idx_watchlist_df
        df = df_idx if df.is_empty() else pl.concat([df, df_idx], how="diagonal_relaxed")

    # as_of 取三类缓存中较旧者
    dates = [d for d in (cache_date if stock_symbols else None, etf_date, index_date) if d is not None]
    as_of = min(dates) if dates else None
    if df.is_empty():
        return {"rows": [], "as_of": str(as_of) if as_of else None, "elapsed_ms": 0}

    # JOIN float_shares (仅股票有) + 名称 (股票/ETF 统一走 get_name_map)
    df_i = repo.get_instruments()
    if not df_i.is_empty() and "float_shares" in df_i.columns:
        df = df.join(df_i.select(["symbol", "float_shares"]), on="symbol", how="left")
    name_map = repo.get_name_map(df["symbol"].to_list())
    df = df.with_columns(
        pl.col("symbol").replace_strict(name_map, default=None, return_dtype=pl.Utf8).alias("name")
    )

    # 标注资产类型: 前端据此渲染徽标/豁免板块筛选/分时列降级
    asset_map = {**{s: "etf" for s in etf_symbols}, **{s: "index" for s in index_symbols}}
    df = df.with_columns(
        pl.col("symbol").replace_strict(asset_map, default="stock", return_dtype=pl.Utf8).alias("asset_type")
    )

    # 选择内置需要的列
    keep = [c for c in _WATCHLIST_COLS + ["name", "float_shares", "asset_type"] if c in df.columns]
    df = df.select(keep)

    # 动态 JOIN 扩展数据表
    ext_specs = _parse_ext_columns(ext_columns) if ext_columns else []
    if ext_specs:
        db = repo.store.db
        data_dir = repo.store.data_dir
        from app.services.ext_data import ExtConfigStore
        from app.api.ext_data import _read_ext_dataframe

        ext_store = ExtConfigStore(data_dir)
        configs = {c.id: c for c in ext_store.load_all()}

        for config_id, field_name in ext_specs:
            view_name = f"ext_{config_id}"
            ext_col_name = f"{config_id}__{field_name}"
            try:
                # 扩展时序数据必须只取最新分区；否则一个 symbol 会按历史分区数被 JOIN 放大。
                cfg = configs.get(config_id)
                if cfg:
                    ext_df, _ = _read_ext_dataframe(cfg, data_dir)
                else:
                    ext_df = pl.from_arrow(db.query(
                        f"SELECT symbol, {quote_ident(field_name)} FROM {view_name}"
                    ).arrow())
                if not ext_df.is_empty() and "symbol" in ext_df.columns:
                    ext_df = (
                        ext_df
                        .select(["symbol", field_name])
                        .unique(subset=["symbol"], keep="last")
                        .rename({field_name: ext_col_name})
                    )
                    df = df.join(ext_df.select(["symbol", ext_col_name]), on="symbol", how="left")
            except Exception:
                # view 不存在或字段不存在，尝试直接读 parquet
                cfg = configs.get(config_id)
                if cfg:
                    try:
                        ext_df, _ = _read_ext_dataframe(cfg, data_dir)
                        if not ext_df.is_empty() and "symbol" in ext_df.columns and field_name in ext_df.columns:
                            ext_df = (
                                ext_df
                                .select(["symbol", field_name])
                                .unique(subset=["symbol"], keep="last")
                                .rename({field_name: ext_col_name})
                            )
                            df = df.join(ext_df, on="symbol", how="left")
                    except Exception as e2:
                        logger.debug("ext join fallback failed for %s.%s: %s", config_id, field_name, e2)

    # sanitize NaN / Inf
    float_cols = [c for c in df.columns if df[c].dtype.is_float()]
    if float_cols:
        df = df.with_columns([
            pl.when(pl.col(c).is_nan() | pl.col(c).is_infinite())
              .then(None)
              .otherwise(pl.col(c))
              .alias(c)
            for c in float_cols
        ])

    # 按板块内顺序重排行
    order_map = {s: i for i, s in enumerate(symbols)}
    df = df.with_columns(pl.col("symbol").map_elements(lambda s: order_map.get(s, len(symbols)), return_dtype=pl.Int32).alias("_sort_order"))
    df = df.sort("_sort_order").drop("_sort_order")

    rows = df.to_dicts()
    elapsed = (time.perf_counter() - t0) * 1000
    return {"rows": rows, "as_of": str(as_of) if as_of else None, "elapsed_ms": elapsed}


def _parse_ext_columns(ext_columns: str) -> list[tuple[str, str]]:
    """解析 "config_id1.field1,config_id2.field2" 为 [(config_id, field_name), ...]"""
    result = []
    for part in ext_columns.split(","):
        part = part.strip()
        if "." not in part:
            continue
        config_id, field_name = part.split(".", 1)
        config_id = config_id.strip()
        field_name = field_name.strip()
        if config_id and field_name and is_valid_ext_ident(config_id):
            result.append((config_id, field_name))
    return result


# ========== 设置相关 API ==========

@router.get("/settings")
def get_settings():
    try:
        return {
            "view_mode": preferences.get_watchlist_groups_view_mode(),
            "display_style": preferences.get_watchlist_groups_display_style(),
            "avg_pct_mode": preferences.get_watchlist_groups_avg_pct_mode(),
        }
    except Exception as e:
        logger.exception("Error getting settings")
        raise HTTPException(500, f"获取设置失败: {str(e)}") from e


@router.post("/settings/view-mode")
def set_view_mode(req: SetViewModeRequest):
    try:
        mode = preferences.set_watchlist_groups_view_mode(req.mode)
        return {"view_mode": mode}
    except Exception as e:
        logger.exception("Error setting view mode")
        raise HTTPException(500, f"设置视图模式失败: {str(e)}") from e


@router.post("/settings/display-style")
def set_display_style(req: SetDisplayStyleRequest):
    try:
        style = preferences.set_watchlist_groups_display_style(req.style)
        return {"display_style": style}
    except Exception as e:
        logger.exception("Error setting display style")
        raise HTTPException(500, f"设置展示样式失败: {str(e)}") from e


@router.post("/settings/avg-pct-mode")
def set_avg_pct_mode(req: SetAvgPctModeRequest):
    try:
        mode = preferences.set_watchlist_groups_avg_pct_mode(req.mode)
        return {"avg_pct_mode": mode}
    except Exception as e:
        logger.exception("Error setting avg pct mode")
        raise HTTPException(500, f"设置平均涨跌幅模式失败: {str(e)}") from e
