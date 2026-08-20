"""情绪周期(sentiment)计算 — 纯函数模块。

职责: 从已算好的 enriched 数据(含信号列)按日聚合情绪指标,
用 market_overview_builder 的评分模型计算 6 维度分 + 总情绪分,
持久化为时序表。不重算指标(不走 compute_indicators), 不依赖 quote/depth_service。

设计原则:
- 每个历史日期只用该日期当天的数据, 不依赖未来数据
- 完全复用 market_overview_builder 的评分逻辑
- 存储所有计算所需的原始指标, 而非仅 6 个维度分
"""
from __future__ import annotations

import logging
import math
import re
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from app.services.ext_data import ExtConfig, ExtConfigStore

logger = logging.getLogger(__name__)

_DIMENSION_SEP = re.compile(r"[、,，;；|/\s]+")


def _finite(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _dimension_field(config: ExtConfig, kind: str) -> str | None:
    candidates = ["概念", "concept", "theme"] if kind == "concept" else ["行业", "industry", "sector"]
    for candidate in candidates:
        needle = candidate.lower()
        for field in config.fields:
            haystack = f"{field.name} {field.label}".lower()
            if needle in haystack:
                return field.name
    return None


def _ext_files(data_dir: Path, config: ExtConfig) -> list[str]:
    base = data_dir / "ext_data" / config.id
    if config.mode == "timeseries":
        root = base / "timeseries"
        return [str(p) for p in sorted(root.rglob("*.parquet")) if p.is_file()]
    return [str(p) for p in sorted(base.glob("*.parquet")) if p.is_file()]


def _read_ext_rows(data_dir, config: ExtConfig, dimension_field: str) -> list[dict]:
    files = _ext_files(data_dir, config)
    if not files:
        return []
    try:
        df = pl.read_parquet(files, hive_partitioning=True)
    except TypeError:
        try:
            df = pl.read_parquet(files)
        except Exception:  # noqa: BLE001
            return []
    except Exception:  # noqa: BLE001
        return []
    if df.is_empty() or dimension_field not in df.columns:
        return []

    if config.mode == "timeseries" and "date" in df.columns:
        latest = df.get_column("date").max()
        if latest is not None:
            df = df.filter(pl.col("date") == latest)

    symbol_cols = ["symbol", "code", "股票代码", "代码"]
    for mapping in (config.symbol_map, config.code_map):
        if isinstance(mapping, dict) and mapping.get("type") == "mapped" and mapping.get("col"):
            symbol_cols.append(str(mapping["col"]))
    cols = []
    for col in [dimension_field, *symbol_cols]:
        if col in df.columns and col not in cols:
            cols.append(col)
    return df.select(cols).to_dicts()


def _dimension_values(raw: Any) -> list[str]:
    if raw is None:
        return []
    values = [
        v.strip()
        for v in _DIMENSION_SEP.split(str(raw).strip())
        if v.strip() and v.strip().casefold() not in {"nan", "none", "null"}
    ]
    return values


def _symbol_keys(row: dict, config: ExtConfig) -> list[str]:
    fields = ["symbol", "code", "股票代码", "代码"]
    for mapping in (config.symbol_map, config.code_map):
        if isinstance(mapping, dict) and mapping.get("type") == "mapped" and mapping.get("col"):
            fields.append(str(mapping["col"]))

    keys: list[str] = []
    for field in fields:
        raw = row.get(field)
        if raw is None:
            continue
        text = str(raw).strip().upper()
        if not text:
            continue
        keys.append(text)
        if "." in text:
            keys.append(text.split(".", 1)[0])
    return keys


def _dimension_rank(rows: list[dict], repo, kind: str, limit: int = 5, level: int | None = None) -> dict:
    if not rows:
        return {"leading": [], "lagging": []}

    quote_map: dict[str, dict] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        quote_map[symbol] = row
        quote_map[symbol.split(".", 1)[0]] = row

    logger.debug(f"_dimension_rank kind={kind}: quote_map has {len(quote_map)} entries")

    store = ExtConfigStore(repo.store.data_dir)
    groups: dict[str, dict[str, dict]] = {}
    configs = store.load_all()
    logger.debug(f"_dimension_rank kind={kind}: found {len(configs)} configs")
    for config in configs:
        field = _dimension_field(config, kind)
        if not field:
            continue
        logger.debug(f"_dimension_rank kind={kind}: using config {config.id}, field={field}")
        ext_rows = _read_ext_rows(repo.store.data_dir, config, field)
        logger.debug(f"_dimension_rank kind={kind}: read {len(ext_rows)} ext_rows")
        for ext_row in ext_rows:
            quote = None
            for key in _symbol_keys(ext_row, config):
                quote = quote_map.get(key)
                if quote:
                    break
            if not quote:
                continue
            symbol = str(quote.get("symbol") or "")
            for value in _dimension_values(ext_row.get(field)):
                # 行业按 "-" 拆分级: "银行-银行-股份制银行" → level=2 取"银行"(二级)
                if level is not None and "-" in value:
                    parts = value.split("-")
                    value = parts[level - 1] if level <= len(parts) else parts[-1]
                groups.setdefault(value, {})[symbol] = quote
    logger.debug(f"_dimension_rank kind={kind}: groups has {len(groups)} entries")

    items = []
    for name, by_symbol in groups.items():
        stocks = list(by_symbol.values())
        changes = [_finite(s.get("change_pct")) for s in stocks]
        changes = [v for v in changes if v is not None]
        if not changes:
            continue
        items.append({
            "name": name,
            "count": len(stocks),
            "avg_pct": sum(changes) / len(changes),
        })

    leading = sorted(items, key=lambda x: x["avg_pct"], reverse=True)[:limit]
    lagging = sorted(items, key=lambda x: x["avg_pct"])[:limit]
    return {"leading": leading, "lagging": lagging}


def _score(value: float, low: float, high: float) -> int:
    """归一化: 把 value 在 [low, high] 区间线性映射到 [0, 100], 钳制边界。

    与看板 market_overview_builder._score 完全一致。
    """
    if high <= low:
        return 50
    return max(0, min(100, round((value - low) / (high - low) * 100)))


def _compute_sentiment_scores(metrics: dict) -> dict:
    """计算 6 个子维度分 + 总情绪分 + 情绪标签。

    完全复用 market_overview_builder 的评分逻辑, 权重和归一化区间完全一致。

    metrics 期望字段(由 _aggregate_daily 聚合):
        avg_index_pct, up_pct, avg_pct, median_pct, strong_diff_pct,
        avg_vol_ratio, high_vol_pct, limit_up, seal_rate, max_boards,
        tier2_count, down_pct, strong_down_pct, mainline_avg, mainline_cover_pct
    """
    # 指数维度
    index_score = _score(metrics.get("avg_index_pct", 0), -2.5, 2.5)

    # 赚钱维度
    profit_score = round(
        _score(metrics.get("up_pct", 50), 20, 80) * 0.45
        + _score(metrics.get("avg_pct", 0), -0.02, 0.02) * 0.25
        + _score(metrics.get("median_pct", 0), -0.02, 0.02) * 0.20
        + _score(metrics.get("strong_diff_pct", 0), -8, 8) * 0.10
    )

    # 量能维度
    money_score = round(
        _score(metrics.get("avg_vol_ratio", 1), 0.6, 1.8) * 0.70
        + _score(metrics.get("high_vol_pct", 5), 2, 12) * 0.30
    )

    # 投机维度
    speculation_score = round(
        _score(metrics.get("limit_up", 0), 5, 90) * 0.25
        + _score(metrics.get("seal_rate", 50), 30, 85) * 0.35
        + _score(metrics.get("max_boards", 0), 1, 8) * 0.25
        + _score(metrics.get("tier2_count", 0), 0, 30) * 0.15
    )

    # 抗跌维度
    resilience_score = 100 - round(
        _score(metrics.get("down_pct", 50), 20, 80) * 0.55
        + _score(metrics.get("strong_down_pct", 5), 1, 12) * 0.45
    )

    # 主线维度
    mainline_score = round(
        _score(metrics.get("mainline_avg", 0), -0.005, 0.03) * 0.65
        + _score(metrics.get("mainline_cover_pct", 0), 1, 12) * 0.35
    ) if metrics.get("mainline_avg") is not None else 50

    # 总情绪分(6 维度简单平均)
    emotion_score = round(
        (index_score + profit_score + money_score + speculation_score + resilience_score + mainline_score) / 6
    )

    # 情绪标签
    if emotion_score >= 70:
        emotion_label = "强势"
    elif emotion_score >= 55:
        emotion_label = "偏暖"
    elif emotion_score >= 45:
        emotion_label = "震荡"
    elif emotion_score >= 30:
        emotion_label = "偏冷"
    else:
        emotion_label = "冰点"

    return {
        "index_score": index_score,
        "profit_score": profit_score,
        "money_score": money_score,
        "speculation_score": speculation_score,
        "resilience_score": resilience_score,
        "mainline_score": mainline_score,
        "emotion_score": emotion_score,
        "emotion_label": emotion_label,
    }


# ───────────────────────── 批量聚合 ─────────────────────────

def _aggregate_daily(df: pl.DataFrame, index_pct_map: dict | None = None, repo=None) -> pl.DataFrame:
    """对多日多 symbol 的 enriched DataFrame 按 date 聚合情绪指标。

    纯 polars 聚合, 不重算指标(假设 df 已含所需列)。
    index_pct_map: {date: {symbol: pct}} 可选, 由调用方预先计算。
    repo: 用于计算主线维度的概念/行业排名。
    """
    needed = ["date", "symbol", "change_pct", "amount", "signal_limit_up",
              "signal_broken_limit_up", "consecutive_limit_ups",
              "close", "ma5", "ma20", "ma60", "high_60d", "low_60d",
              "signal_n_day_high", "signal_n_day_low", "turnover_rate",
              "vol_ratio_5d"]
    avail = [c for c in needed if c in df.columns]
    if "date" not in avail or "change_pct" not in avail:
        return pl.DataFrame()

    # 先计算 tier2_count: 需要按日期分组,然后对每个日期,统计 consecutive_limit_ups >= 2 的股票数
    tier2_count_by_date = None
    if "consecutive_limit_ups" in df.columns:
        tier2_count_by_date = (
            df
            .filter(pl.col("consecutive_limit_ups") >= 2)
            .group_by("date")
            .agg(pl.col("consecutive_limit_ups").count().alias("tier2_count"))
        )

    # 基础聚合: 全部用 group_by 一次性向量化算出
    agg_exprs = [
        pl.col("change_pct").gt(0).sum().alias("up_count"),
        pl.col("change_pct").lt(0).sum().alias("down_count"),
        pl.len().alias("total_count"),
        pl.col("change_pct").mean().alias("avg_pct"),
        pl.col("change_pct").median().alias("median_pct"),
        pl.col("change_pct").ge(0.03).sum().alias("strong_up_count"),
        pl.col("change_pct").le(-0.03).sum().alias("strong_down_count"),
    ]
    if "vol_ratio_5d" in df.columns:
        agg_exprs.append(pl.col("vol_ratio_5d").mean().alias("avg_vol_ratio"))
        agg_exprs.append(pl.col("vol_ratio_5d").ge(1.5).sum().alias("high_vol_ratio_count"))
    if "turnover_rate" in df.columns:
        agg_exprs.append(pl.col("turnover_rate").mean().alias("avg_turnover"))
    if "signal_limit_up" in df.columns:
        agg_exprs.append(pl.col("signal_limit_up").cast(pl.Boolean).sum().alias("limit_up"))
    if "signal_broken_limit_up" in df.columns:
        agg_exprs.append(pl.col("signal_broken_limit_up").cast(pl.Boolean).sum().alias("broken_limit"))
    if "consecutive_limit_ups" in df.columns:
        agg_exprs.append(pl.col("consecutive_limit_ups").max().alias("max_boards"))
    
    grouped = df.group_by("date").agg(*agg_exprs).sort("date")

    # 把 tier2_count 合并进来
    if tier2_count_by_date is not None:
        grouped = grouped.join(tier2_count_by_date, on="date", how="left").fill_null(0)


    # 转成 dict 列表逐天处理
    rows = []
    index_pct_map = index_pct_map or {}
    for r in grouped.iter_rows(named=True):
        total = r.get("total_count", 0) or 0
        if total == 0:
            continue

        up = r.get("up_count", 0) or 0
        down = r.get("down_count", 0) or 0
        up_pct = up / total * 100
        down_pct = down / total * 100
        strong_up = r.get("strong_up_count", 0) or 0
        strong_down = r.get("strong_down_count", 0) or 0
        strong_up_pct = strong_up / total * 100
        strong_down_pct = strong_down / total * 100
        strong_diff_pct = strong_up_pct - strong_down_pct

        avg_pct = r.get("avg_pct", 0) or 0
        median_pct = r.get("median_pct", 0) or 0
        limit_up = r.get("limit_up", 0) or 0
        broken = r.get("broken_limit", 0) or 0
        seal_rate = limit_up / (limit_up + broken) * 100 if (limit_up + broken) > 0 else 0
        max_boards = r.get("max_boards", 0) or 0
        avg_vol_ratio = r.get("avg_vol_ratio", 1) or 1
        high_vol_ratio_count = r.get("high_vol_ratio_count", 0) or 0
        high_vol_pct = high_vol_ratio_count / total * 100
        tier2_count = r.get("tier2_count", 0) or 0

        # 指数涨幅: 主力指数平均涨幅 - index_pct_map 中的值已经是百分比形式,直接使用即可
        index_pct_for_date = index_pct_map.get(r["date"], {})
        avg_index_pct = (
            sum(v for v in index_pct_for_date.values() if v is not None) / len(index_pct_for_date)
            if index_pct_for_date
            else 0
        )

        # 主线维度: 计算概念/行业排名
        mainline_avg = 0.0
        mainline_cover_pct = 0.0
        if repo is not None:
            # 获取当天的原始 enriched 数据
            day_df = df.filter(pl.col("date") == r["date"])
            if not day_df.is_empty():
                day_rows = day_df.to_dicts()
                logger.debug(f"Date {r['date']}: {len(day_rows)} day_rows available")
                concept_rank = _dimension_rank(day_rows, repo, "concept")
                industry_rank = _dimension_rank(day_rows, repo, "industry", level=2)
                logger.debug(f"Date {r['date']}: concept_rank leading={len(concept_rank['leading'])}, industry_rank leading={len(industry_rank['leading'])}")
                mainline_items = [*concept_rank["leading"][:3], *industry_rank["leading"][:3]]
                logger.debug(f"Date {r['date']}: {len(mainline_items)} mainline_items")
                if mainline_items:
                    mainline_avg = max([_finite(item.get("avg_pct")) or 0 for item in mainline_items], default=0)
                    mainline_cover_pct = max([(_finite(item.get("count")) or 0) / total * 100 for item in mainline_items], default=0) if total else 0
                    logger.debug(f"Date {r['date']}: mainline_avg={mainline_avg}, mainline_cover_pct={mainline_cover_pct}")

        metrics = {
            "date": r["date"],
            "avg_index_pct": avg_index_pct,
            "up_pct": up_pct,
            "avg_pct": avg_pct,
            "median_pct": median_pct,
            "strong_diff_pct": strong_diff_pct,
            "avg_vol_ratio": avg_vol_ratio,
            "high_vol_pct": high_vol_pct,
            "limit_up": limit_up,
            "seal_rate": seal_rate,
            "max_boards": max_boards,
            "tier2_count": tier2_count,
            "down_pct": down_pct,
            "strong_down_pct": strong_down_pct,
            "mainline_avg": mainline_avg,
            "mainline_cover_pct": mainline_cover_pct,
        }

        # 计算评分
        scores = _compute_sentiment_scores(metrics)

        # 组合最终行
        rows.append({
            "date": r["date"],
            # 原始指标
            "avg_index_pct": round(avg_index_pct, 4),
            "up_pct": round(up_pct, 4),
            "avg_pct": round(avg_pct, 4),
            "median_pct": round(median_pct, 4),
            "strong_diff_pct": round(strong_diff_pct, 4),
            "avg_vol_ratio": round(avg_vol_ratio, 4),
            "high_vol_pct": round(high_vol_pct, 4),
            "limit_up": limit_up,
            "seal_rate": round(seal_rate, 4),
            "max_boards": max_boards,
            "tier2_count": tier2_count,
            "down_pct": round(down_pct, 4),
            "strong_down_pct": round(strong_down_pct, 4),
            "mainline_avg": round(mainline_avg, 4),
            "mainline_cover_pct": round(mainline_cover_pct, 4),
            # 计算结果
            "index_score": scores["index_score"],
            "profit_score": scores["profit_score"],
            "money_score": scores["money_score"],
            "speculation_score": scores["speculation_score"],
            "resilience_score": scores["resilience_score"],
            "mainline_score": scores["mainline_score"],
            "emotion_score": scores["emotion_score"],
            "emotion_label": scores["emotion_label"],
        })

    return pl.DataFrame(rows) if rows else pl.DataFrame()


# 全量回填分批参数(控制内存峰值) —— 实际值从用户偏好读取
_SENTIMENT_BATCH_DAYS_DEFAULT = 60
_SENTIMENT_WARMUP_DAYS_DEFAULT = 0  # 不需要预热, 逐日独立计算


def _compute_batch(repo, enriched_dir, instruments, historical_shares,
                   batch_start: date, batch_end: date) -> pl.DataFrame:
    """单批: 读 enriched 数据 → 算指标 → 返回目标区间 DataFrame。

    sentiment 不需要 warmup, 因为不依赖滚动窗口指标(如 ma20)。
    """
    from app.indicators.pipeline import compute_indicators, compute_limit_signals
    df = pl.scan_parquet(enriched_dir / "**" / "*.parquet").filter(
        (pl.col("date") >= batch_start) & (pl.col("date") <= batch_end)
    ).collect()
    if df.is_empty():
        return pl.DataFrame()
    # 计算所需指标
    df = compute_indicators(df, needed={"change_pct", "vol_ratio_5d", "turnover_rate",
                                        "ma5", "ma20", "ma60", "high_60d", "low_60d"})
    if instruments is not None and not instruments.is_empty():
        df = compute_limit_signals(
            df, instruments,
            needed={"signal_limit_up", "signal_limit_down", "signal_broken_limit_up",
                    "consecutive_limit_ups", "signal_n_day_high", "signal_n_day_low"},
            historical_shares=historical_shares,
        )
    return df


def _scan_enriched_fallback(repo, start: date, end: date) -> pl.DataFrame | None:
    """缓存不覆盖时的慢路径: scan enriched parquet + 重算所需指标列。

    与 regime_builder 类似, 但不需要 ma20 等需要预热的指标。
    """
    try:
        from app.services import preferences
        batch_days = preferences.get_sentiment_batch_days()
    except Exception:  # noqa: BLE001
        batch_days = _SENTIMENT_BATCH_DAYS_DEFAULT

    try:
        enriched_dir = repo.store.data_dir / "kline_daily_enriched"
        if not enriched_dir.exists():
            return None
        instruments = repo.get_instruments()
        historical_shares = repo.get_historical_shares()

        # 收集目标区间内所有交易日
        target_dates = sorted(d for d in enriched_date_set(repo) if start <= d <= end)
        if not target_dates:
            return None

        # 小范围: 单次算
        if len(target_dates) <= batch_days:
            df = _compute_batch(repo, enriched_dir, instruments, historical_shares,
                                target_dates[0], target_dates[-1])
            return df if not df.is_empty() else None

        # 大范围: 按交易日分批
        batches = [
            (target_dates[i], target_dates[min(i + batch_days - 1, len(target_dates) - 1)])
            for i in range(0, len(target_dates), batch_days)
        ]
        logger.info("sentiment fallback: %d days, %d batches", len(target_dates), len(batches))
        parts: list[pl.DataFrame] = []
        for bs, be in batches:
            df = _compute_batch(repo, enriched_dir, instruments, historical_shares, bs, be)
            if not df.is_empty():
                parts.append(df)
        if not parts:
            return None
        return pl.concat(parts, how="vertical_relaxed")
    except Exception as e:  # noqa: BLE001
        logger.warning("sentiment scan_enriched_fallback failed: %s", e)
        return None


def _load_index_pct_map(repo, start: date, end: date) -> dict:
    """读取主力指数日K, 计算每日各指数涨幅 → {date: {symbol: pct}}。"""
    from app.services.market_overview_builder import CORE_INDEX_SYMBOLS

    index_pct_map: dict = {}
    try:
        for symbol in CORE_INDEX_SYMBOLS:
            df = repo.get_index_daily(symbol, start, end, columns=["date", "change_pct"])
            if df.is_empty() or "change_pct" not in df.columns:
                continue
            for r in df.iter_rows(named=True):
                d = r["date"]
                pct = float(r.get("change_pct") or 0) * 100  # 小数转百分比
                if d not in index_pct_map:
                    index_pct_map[d] = {}
                index_pct_map[d][symbol] = pct
    except Exception as e:  # noqa: BLE001
        logger.warning("sentiment load_index_pct_map failed: %s", e)
    return index_pct_map


def run_sentiment_batch(repo, start: date, end: date) -> pl.DataFrame:
    """批算 [start, end] 的情绪周期时序。

    性能: 优先 repo.get_enriched_range(内存缓存); 缓存不覆盖走 scan_parquet 慢路径。
    按 date group_by 聚合, 不逐日重算。返回完整时序 DataFrame(可能为空)。
    """
    if start > end:
        logger.warning("run_sentiment_batch: start > end (%s > %s)", start, end)
        return pl.DataFrame()

    logger.info("run_sentiment_batch: computing sentiment from %s to %s", start, end)

    # 指数涨幅(所有主力指数)
    index_pct_map = _load_index_pct_map(repo, start, end)
    logger.info("run_sentiment_batch: loaded %d index_pct entries", len(index_pct_map))

    # enriched 多日数据(优先缓存)
    df = repo.get_enriched_range(start, end)
    if df is None or df.is_empty():
        logger.info("sentiment batch: enriched cache miss [%s~%s], fallback to scan", start, end)
        df = _scan_enriched_fallback(repo, start, end)
    
    if df is None or df.is_empty():
        logger.warning("sentiment batch: no enriched data for [%s~%s]", start, end)
        return pl.DataFrame()
    
    logger.info("run_sentiment_batch: loaded enriched data with %d rows", df.height)
    
    # 检查是否有需要的 limit signals 列,如果没有就计算
    needed_limit_cols = ["signal_limit_up", "signal_broken_limit_up", "consecutive_limit_ups"]
    has_limit_cols = all(col in df.columns for col in needed_limit_cols)
    if not has_limit_cols:
        logger.info("run_sentiment_batch: limit signals columns missing, computing them now")
        try:
            from app.indicators.pipeline import compute_limit_signals
            instruments = repo.get_instruments()
            historical_shares = repo.get_historical_shares()
            df = compute_limit_signals(
                df,
                instruments,
                needed=needed_limit_cols,
                historical_shares=historical_shares,
            )
        except Exception as e:
            logger.warning("run_sentiment_batch: failed to compute limit signals: %s", e)
    
    result = _aggregate_daily(df, index_pct_map, repo)
    logger.info("run_sentiment_batch: aggregated %d rows", result.height if not result.is_empty() else 0)
    
    return result


# ───────────────────────── 持久化(upsert) ─────────────────────────

SENTIMENT_DIR = "sentiment_history"


def sentiment_path(data_dir: Path) -> Path:
    return data_dir / SENTIMENT_DIR / "part.parquet"


def load_sentiment_history(data_dir: Path) -> pl.DataFrame:
    """读取全部 sentiment 时序; 不存在返回空 DataFrame。"""
    p = sentiment_path(data_dir)
    if not p.exists():
        return pl.DataFrame()
    try:
        return pl.read_parquet(p)
    except Exception as e:  # noqa: BLE001
        logger.warning("load_sentiment_history failed: %s", e)
        return pl.DataFrame()


def upsert_sentiment_history(data_dir: Path, new_rows: pl.DataFrame) -> None:
    """按 date 覆盖(upsert): 重算的天覆盖旧行, 新天追加。

    与 regime_builder.upsert_regime_history 逻辑完全一致。
    """
    if new_rows.is_empty() or "date" not in new_rows.columns:
        return
    p = sentiment_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    new_dates = set(new_rows["date"].to_list())
    old = load_sentiment_history(data_dir)
    if old.is_empty():
        combined = new_rows
    else:
        kept = old.filter(~pl.col("date").is_in(list(new_dates)))
        # schema 对齐: 以 new_rows 的列名+顺序为权威
        target_cols = new_rows.columns
        keep_exprs = []
        for c in target_cols:
            if c in kept.columns:
                keep_exprs.append(pl.col(c))
            else:
                keep_exprs.append(pl.lit(None).alias(c))
        kept = kept.select(keep_exprs)
        new_rows = new_rows.select(target_cols)
        combined = pl.concat([kept, new_rows], how="vertical_relaxed")
    combined = combined.sort("date").unique(subset=["date"], keep="last")
    combined.write_parquet(p)


def get_sentiment_coverage(data_dir: Path) -> dict:
    """返回 sentiment 时序的覆盖元信息(供数据画像/API)。"""
    df = load_sentiment_history(data_dir)
    if df.is_empty():
        return {"rows": 0, "earliest_date": None, "latest_date": None}
    return {
        "rows": df.height,
        "earliest_date": str(df["date"].min()),
        "latest_date": str(df["date"].max()),
    }


def detect_stale_dates(data_dir: Path, repo) -> list[date]:
    """检测 sentiment 已有但需要重算的天(enriched 被覆写)。

    与 regime_builder.detect_stale_dates 逻辑完全一致。
    """
    sentiment_p = sentiment_path(data_dir)
    if not sentiment_p.exists():
        return []
    sentiment_mtime = sentiment_p.stat().st_mtime
    enriched_dir = repo.store.data_dir / "kline_daily_enriched"
    if not enriched_dir.exists():
        return []
    stale: list[date] = []
    existing = load_sentiment_history(data_dir)
    if existing.is_empty():
        return []
    existing_dates = set(existing["date"].to_list())
    for part in enriched_dir.glob("date=*/part.parquet"):
        try:
            ds = part.parent.name.replace("date=", "")
            d = date.fromisoformat(ds)
        except (ValueError, OSError):
            continue
        if d not in existing_dates:
            continue
        try:
            if part.stat().st_mtime > sentiment_mtime:
                stale.append(d)
        except OSError:
            continue
    return sorted(stale)


def compute_sentiment_incremental(repo, data_dir: Path, *, today: date | None = None) -> pl.DataFrame:
    """增量计算 sentiment(供 daily_pipeline / 启动补算调用)。

    与 regime_builder.compute_regime_incremental 逻辑完全一致。
    """
    today = today or date.today()
    existing = load_sentiment_history(data_dir)

    # 缺口: enriched 有哪些天, sentiment 缺哪些
    enriched_dates = enriched_date_set(repo)
    existing_dates = set(existing["date"].to_list()) if not existing.is_empty() else set()
    missing = sorted(d for d in enriched_dates if d not in existing_dates and d <= today)

    # stale: enriched 覆写过
    stale = detect_stale_dates(data_dir, repo)

    to_compute = sorted(set(missing) | set(stale))
    if not to_compute:
        logger.debug("sentiment incremental: nothing to compute")
        return pl.DataFrame()

    logger.info("sentiment incremental: compute %d days (missing=%d, stale=%d)",
                len(to_compute), len(missing), len(stale))
    new_rows = run_sentiment_batch(repo, start=to_compute[0], end=to_compute[-1])
    if not new_rows.is_empty():
        upsert_sentiment_history(data_dir, new_rows)
    return new_rows


def enriched_date_set(repo) -> set[date]:
    """扫描 kline_daily_enriched 分区目录, 返回所有已有日期集合)

    复用 regime_builder 中的同名函数。
    """
    enriched_dir = repo.store.data_dir / "kline_daily_enriched"
    dates: set[date] = set()
    if not enriched_dir.exists():
        logger.warning("enriched_date_set: enriched_dir does not exist: %s", enriched_dir)
        return dates
    
    logger.info("enriched_date_set: scanning enriched_dir: %s", enriched_dir)
    part_count = 0
    for part in enriched_dir.glob("date=*/part.parquet"):
        part_count += 1
        try:
            ds = part.parent.name.replace("date=", "")
            dates.add(date.fromisoformat(ds))
        except ValueError:
            continue
    
    logger.info("enriched_date_set: found %d part files, %d dates", part_count, len(dates))
    return dates


def earliest_enriched_date(repo) -> date | None:
    """返回 enriched 最早日期(供全量重算定起点)。无数据返回 None)

    复用 regime_builder 中的同名函数)
    """
    dates = enriched_date_set(repo)
    if dates:
        earliest = min(dates)
        logger.info("earliest_enriched_date: %s", earliest)
        return earliest
    else:
        logger.warning("earliest_enriched_date: no dates found")
        return None
