"""自选股板块服务。

存储:
- data/user_data/watchlist_groups.parquet: 板块列表 (group_id, name, order, created_at)
- data/user_data/watchlist_group_items.parquet: 板块-股票关联 (group_id, symbol, order, added_at, note)
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
import uuid

import polars as pl

from app.config import settings

logger = logging.getLogger(__name__)


def _groups_path() -> Path:
    p = settings.data_dir / "user_data" / "watchlist_groups.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _items_path() -> Path:
    p = settings.data_dir / "user_data" / "watchlist_group_items.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _handle_corrupted_file(p: Path) -> None:
    """尝试删除损坏的文件。"""
    if p.exists():
        try:
            p.unlink()
            logger.info(f"Removed corrupted file: {p}")
        except OSError as e:
            logger.warning(f"Failed to remove corrupted file {p}: {e}")


def list_groups() -> list[dict]:
    """列出所有板块，按 order 排序。"""
    p = _groups_path()
    if not p.exists():
        return []
    try:
        df = pl.read_parquet(p)
        if df.is_empty():
            return []
        # 确保必要的列存在
        required_cols = ["group_id", "name", "order"]
        for col in required_cols:
            if col not in df.columns:
                logger.warning(f"Missing required column '{col}' in groups file, will recreate")
                _handle_corrupted_file(p)
                return []
        return df.sort("order").to_dicts()
    except pl.exceptions.PolarsError as e:
        logger.warning(f"Parquet parsing error for groups file: {e}, will try to remove corrupted file")
    except OSError as e:
        logger.warning(f"OS error reading groups file: {e}, will try to remove corrupted file")
    except Exception as e:
        logger.warning(f"Unexpected error reading groups file: {e}", exc_info=True)
    
    _handle_corrupted_file(p)
    return []


def _get_group_by_name(name: str) -> dict | None:
    """按名称查找板块（用于唯一性校验）。"""
    name = name.strip()
    p = _groups_path()
    if not p.exists():
        return None
    try:
        df = pl.read_parquet(p)
        if df.is_empty():
            return None
        if "name" not in df.columns:
            return None
        matches = df.filter(pl.col("name") == name)
        if matches.is_empty():
            return None
        return matches.to_dicts()[0]
    except pl.exceptions.PolarsError as e:
        logger.warning(f"Parquet parsing error for groups file: {e}")
    except OSError as e:
        logger.warning(f"OS error reading groups file: {e}")
    except Exception as e:
        logger.warning(f"Unexpected error reading groups file: {e}", exc_info=True)
    return None


def _get_group_by_id(group_id: str) -> dict | None:
    """按 ID 查找板块。"""
    group_id = group_id.strip()
    p = _groups_path()
    if not p.exists():
        return None
    try:
        df = pl.read_parquet(p)
        if df.is_empty():
            return None
        if "group_id" not in df.columns:
            return None
        matches = df.filter(pl.col("group_id") == group_id)
        if matches.is_empty():
            return None
        return matches.to_dicts()[0]
    except pl.exceptions.PolarsError as e:
        logger.warning(f"Parquet parsing error for groups file: {e}")
    except OSError as e:
        logger.warning(f"OS error reading groups file: {e}")
    except Exception as e:
        logger.warning(f"Unexpected error reading groups file: {e}", exc_info=True)
    return None


def create_group(name: str) -> list[dict]:
    """创建板块。"""
    name = name.strip()
    if not name:
        raise ValueError("板块名称不能为空")
    
    # 校验唯一性
    if _get_group_by_name(name) is not None:
        raise ValueError("板块名称已存在")
    
    p = _groups_path()
    try:
        if p.exists():
            df = pl.read_parquet(p)
            # 确保必要的列存在
            required_cols = ["group_id", "name", "order"]
            for col in required_cols:
                if col not in df.columns:
                    logger.warning(f"Missing required column '{col}' in groups file, recreating")
                    df = pl.DataFrame(schema={
                        "group_id": pl.Utf8,
                        "name": pl.Utf8,
                        "order": pl.Int64,
                        "created_at": pl.Utf8
                    })
        else:
            df = pl.DataFrame(schema={
                "group_id": pl.Utf8,
                "name": pl.Utf8,
                "order": pl.Int64,
                "created_at": pl.Utf8
            })
        
        # 计算新 order
        if df.is_empty():
            new_order = 1
        else:
            new_order = df["order"].max() + 1
        
        new_row = pl.DataFrame({
            "group_id": [str(uuid.uuid4())],
            "name": [name],
            "order": [new_order],
            "created_at": [datetime.utcnow().isoformat(timespec="seconds")]
        })
        
        out = pl.concat([df, new_row], how="diagonal_relaxed")
        out.write_parquet(p)
    except pl.exceptions.PolarsError as e:
        logger.error(f"Parquet error creating group: {e}")
        _handle_corrupted_file(p)
        raise ValueError("创建板块时出错，请重试")
    except OSError as e:
        logger.error(f"OS error creating group: {e}")
        _handle_corrupted_file(p)
        raise ValueError("创建板块时出错，请重试")
    except Exception as e:
        logger.error(f"Unexpected error creating group: {e}", exc_info=True)
        _handle_corrupted_file(p)
        raise ValueError("创建板块时出错，请重试")
    
    return list_groups()


def rename_group(group_id: str, new_name: str) -> list[dict]:
    """重命名板块。"""
    group_id = group_id.strip()
    new_name = new_name.strip()
    
    if not new_name:
        raise ValueError("板块名称不能为空")
    
    # 校验板块存在
    existing = _get_group_by_id(group_id)
    if existing is None:
        raise ValueError("板块不存在")
    
    # 校验名称唯一性（排除自己）
    p = _groups_path()
    try:
        df = pl.read_parquet(p)
        others = df.filter(pl.col("group_id") != group_id)
        if new_name in others["name"].to_list():
            raise ValueError("板块名称已存在")
        
        df = df.with_columns(
            pl.when(pl.col("group_id") == group_id)
            .then(pl.lit(new_name))
            .otherwise(pl.col("name"))
            .alias("name")
        )
        
        df.write_parquet(p)
        return list_groups()
    except pl.exceptions.PolarsError as e:
        logger.error(f"Parquet error renaming group: {e}")
        _handle_corrupted_file(p)
        raise ValueError("重命名板块时出错，请重试")
    except OSError as e:
        logger.error(f"OS error renaming group: {e}")
        _handle_corrupted_file(p)
        raise ValueError("重命名板块时出错，请重试")
    except Exception as e:
        logger.error(f"Unexpected error renaming group: {e}", exc_info=True)
        _handle_corrupted_file(p)
        raise ValueError("重命名板块时出错，请重试")


def delete_group(group_id: str) -> list[dict]:
    """删除板块并级联删除关联股票。"""
    group_id = group_id.strip()
    
    try:
        # 删除板块元数据
        p_groups = _groups_path()
        if p_groups.exists():
            df_groups = pl.read_parquet(p_groups)
            df_groups = df_groups.filter(pl.col("group_id") != group_id)
            df_groups.write_parquet(p_groups)
        
        # 级联删除关联股票
        p_items = _items_path()
        if p_items.exists():
            df_items = pl.read_parquet(p_items)
            df_items = df_items.filter(pl.col("group_id") != group_id)
            df_items.write_parquet(p_items)
        
        return list_groups()
    except pl.exceptions.PolarsError as e:
        logger.error(f"Parquet error deleting group: {e}")
        _handle_corrupted_file(p_groups if p_groups.exists() else p_items)
        raise ValueError("删除板块时出错，请重试")
    except OSError as e:
        logger.error(f"OS error deleting group: {e}")
        _handle_corrupted_file(p_groups if p_groups.exists() else p_items)
        raise ValueError("删除板块时出错，请重试")
    except Exception as e:
        logger.error(f"Unexpected error deleting group: {e}", exc_info=True)
        _handle_corrupted_file(p_groups if p_groups.exists() else p_items)
        raise ValueError("删除板块时出错，请重试")


def reorder_groups(group_ids: list[str]) -> list[dict]:
    """重新排序板块。"""
    group_ids = [gid.strip() for gid in group_ids]
    
    p = _groups_path()
    if not p.exists():
        return []
    
    try:
        df = pl.read_parquet(p)
        
        # 构建 order map
        order_map = {gid: i for i, gid in enumerate(group_ids)}
        max_order_in = len(group_ids)
        
        df = df.with_columns(
            pl.when(pl.col("group_id").is_in(group_ids))
            .then(pl.col("group_id").map_elements(lambda x: order_map[x], return_dtype=pl.Int64))
            .otherwise(pl.col("order") + max_order_in)
            .alias("order")
        )
        
        df.write_parquet(p)
        return list_groups()
    except pl.exceptions.PolarsError as e:
        logger.error(f"Parquet error reordering groups: {e}")
        _handle_corrupted_file(p)
        raise ValueError("排序板块时出错，请重试")
    except OSError as e:
        logger.error(f"OS error reordering groups: {e}")
        _handle_corrupted_file(p)
        raise ValueError("排序板块时出错，请重试")
    except Exception as e:
        logger.error(f"Unexpected error reordering groups: {e}", exc_info=True)
        _handle_corrupted_file(p)
        raise ValueError("排序板块时出错，请重试")


def get_groups_with_stats() -> list[dict]:
    """获取所有板块，包含 item_count 统计。"""
    groups = list_groups()
    if not groups:
        return []
    
    counts_dict = {}
    p_items = _items_path()
    if p_items.exists():
        try:
            df_items = pl.read_parquet(p_items)
            if not df_items.is_empty() and "group_id" in df_items.columns and "symbol" in df_items.columns:
                item_counts = df_items.group_by("group_id").agg(pl.col("symbol").count().alias("item_count"))
                counts_dict = {row["group_id"]: row["item_count"] for row in item_counts.to_dicts()}
        except pl.exceptions.PolarsError as e:
            logger.warning(f"Parquet parsing error for items file: {e}")
        except OSError as e:
            logger.warning(f"OS error reading items file: {e}")
        except Exception as e:
            logger.warning(f"Unexpected error reading items file: {e}", exc_info=True)
    
    result = []
    for g in groups:
        g_copy = g.copy()
        g_copy["item_count"] = counts_dict.get(g["group_id"], 0)
        result.append(g_copy)
    
    return result


def list_group_items(group_id: str) -> list[dict]:
    """列出板块内的股票，按 order 排序。"""
    group_id = group_id.strip()
    p = _items_path()
    if not p.exists():
        return []
    try:
        df = pl.read_parquet(p)
        if df.is_empty():
            return []
        # 确保必要的列存在
        required_cols = ["group_id", "symbol", "order"]
        for col in required_cols:
            if col not in df.columns:
                logger.warning(f"Missing required column '{col}' in items file, will recreate")
                _handle_corrupted_file(p)
                return []
        filtered = df.filter(pl.col("group_id") == group_id)
        return filtered.sort("order").to_dicts()
    except pl.exceptions.PolarsError as e:
        logger.warning(f"Parquet parsing error for items file: {e}, will try to remove corrupted file")
    except OSError as e:
        logger.warning(f"OS error reading items file: {e}, will try to remove corrupted file")
    except Exception as e:
        logger.warning(f"Unexpected error reading items file: {e}", exc_info=True)
    
    _handle_corrupted_file(p)
    return []


def add_item(group_id: str, symbol: str, note: str = "") -> list[dict]:
    """向板块添加股票。"""
    group_id = group_id.strip()
    symbol = symbol.strip().upper()
    
    if not symbol:
        raise ValueError("股票代码不能为空")
    
    # 校验板块存在
    if _get_group_by_id(group_id) is None:
        raise ValueError("板块不存在")
    
    p = _items_path()
    try:
        if p.exists():
            df = pl.read_parquet(p)
            # 确保必要的列存在
            required_cols = ["group_id", "symbol", "order"]
            for col in required_cols:
                if col not in df.columns:
                    logger.warning(f"Missing required column '{col}' in items file, recreating")
                    df = pl.DataFrame(schema={
                        "group_id": pl.Utf8,
                        "symbol": pl.Utf8,
                        "order": pl.Int64,
                        "added_at": pl.Utf8,
                        "note": pl.Utf8
                    })
                    break
            # 已存在则先移除
            if "group_id" in df.columns and "symbol" in df.columns:
                df = df.filter(~((pl.col("group_id") == group_id) & (pl.col("symbol") == symbol)))
        else:
            df = pl.DataFrame(schema={
                "group_id": pl.Utf8,
                "symbol": pl.Utf8,
                "order": pl.Int64,
                "added_at": pl.Utf8,
                "note": pl.Utf8
            })
        
        # 计算新 order
        current_items = df.filter(pl.col("group_id") == group_id) if "group_id" in df.columns else pl.DataFrame()
        if current_items.is_empty() or "order" not in current_items.columns:
            new_order = 1
        else:
            new_order = current_items["order"].max() + 1
        
        new_row = pl.DataFrame({
            "group_id": [group_id],
            "symbol": [symbol],
            "order": [new_order],
            "added_at": [datetime.utcnow().isoformat(timespec="seconds")],
            "note": [note]
        })
        
        out = pl.concat([df, new_row], how="diagonal_relaxed")
        out.write_parquet(p)
    except pl.exceptions.PolarsError as e:
        logger.error(f"Parquet error adding item: {e}")
        _handle_corrupted_file(p)
        raise ValueError("添加股票时出错，请重试")
    except OSError as e:
        logger.error(f"OS error adding item: {e}")
        _handle_corrupted_file(p)
        raise ValueError("添加股票时出错，请重试")
    except Exception as e:
        logger.error(f"Unexpected error adding item: {e}", exc_info=True)
        _handle_corrupted_file(p)
        raise ValueError("添加股票时出错，请重试")
    
    return list_group_items(group_id)


def add_items_batch(group_id: str, symbols: list[str], note: str = "") -> tuple[list[dict], int]:
    """批量向板块添加股票。返回 (items, added_count)。"""
    group_id = group_id.strip()
    
    if _get_group_by_id(group_id) is None:
        raise ValueError("板块不存在")
    
    # 清洗 symbol 列表
    cleaned_symbols = []
    seen = set()
    for sym in symbols:
        s = sym.strip().upper()
        if s and s not in seen:
            cleaned_symbols.append(s)
            seen.add(s)
    
    if not cleaned_symbols:
        return [], 0
    
    p = _items_path()
    try:
        if p.exists():
            df = pl.read_parquet(p)
            current_group_items = df.filter(pl.col("group_id") == group_id)
            existing_symbols = set(current_group_items["symbol"].to_list())
            # 过滤已存在的
            to_add = [s for s in cleaned_symbols if s not in existing_symbols]
            # 移除可能已存在的（为了统一处理）
            df = df.filter(~((pl.col("group_id") == group_id) & (pl.col("symbol").is_in(cleaned_symbols))))
        else:
            df = pl.DataFrame(schema={
                "group_id": pl.Utf8,
                "symbol": pl.Utf8,
                "order": pl.Int64,
                "added_at": pl.Utf8,
                "note": pl.Utf8
            })
            to_add = cleaned_symbols
        
        added_count = len(to_add)
        if added_count > 0:
            current_items = df.filter(pl.col("group_id") == group_id)
            if current_items.is_empty():
                base_order = 1
            else:
                base_order = current_items["order"].max() + 1
            
            new_rows = []
            for i, sym in enumerate(to_add):
                new_rows.append({
                    "group_id": group_id,
                    "symbol": sym,
                    "order": base_order + i,
                    "added_at": datetime.utcnow().isoformat(timespec="seconds"),
                    "note": note
                })
            
            df_new = pl.DataFrame(new_rows)
            df = pl.concat([df, df_new], how="diagonal_relaxed")
            df.write_parquet(p)
        
        return list_group_items(group_id), added_count
    except pl.exceptions.PolarsError as e:
        logger.error(f"Parquet error adding items batch: {e}")
        _handle_corrupted_file(p)
        raise ValueError("批量添加股票时出错，请重试")
    except OSError as e:
        logger.error(f"OS error adding items batch: {e}")
        _handle_corrupted_file(p)
        raise ValueError("批量添加股票时出错，请重试")
    except Exception as e:
        logger.error(f"Unexpected error adding items batch: {e}", exc_info=True)
        _handle_corrupted_file(p)
        raise ValueError("批量添加股票时出错，请重试")


def remove_item(group_id: str, symbol: str) -> list[dict]:
    """从板块移除股票。"""
    group_id = group_id.strip()
    symbol = symbol.strip().upper()
    
    p = _items_path()
    if not p.exists():
        return []
    try:
        df = pl.read_parquet(p)
        df = df.filter(~((pl.col("group_id") == group_id) & (pl.col("symbol") == symbol)))
        df.write_parquet(p)
        return list_group_items(group_id)
    except pl.exceptions.PolarsError as e:
        logger.error(f"Parquet error removing item: {e}")
        _handle_corrupted_file(p)
        raise ValueError("移除股票时出错，请重试")
    except OSError as e:
        logger.error(f"OS error removing item: {e}")
        _handle_corrupted_file(p)
        raise ValueError("移除股票时出错，请重试")
    except Exception as e:
        logger.error(f"Unexpected error removing item: {e}", exc_info=True)
        _handle_corrupted_file(p)
        raise ValueError("移除股票时出错，请重试")


def reorder_items(group_id: str, symbols: list[str]) -> list[dict]:
    """重新排序板块内的股票。"""
    group_id = group_id.strip()
    symbols = [s.strip().upper() for s in symbols]
    
    p = _items_path()
    if not p.exists():
        return []
    try:
        df = pl.read_parquet(p)
        
        # 分开处理该板块和其他板块
        this_group = df.filter(pl.col("group_id") == group_id)
        other_groups = df.filter(pl.col("group_id") != group_id)
        
        # 构建 order map
        order_map = {sym: i for i, sym in enumerate(symbols)}
        
        # 更新该板块的 order
        this_group = this_group.with_columns(
            pl.when(pl.col("symbol").is_in(symbols))
            .then(pl.col("symbol").map_elements(lambda x: order_map[x], return_dtype=pl.Int64))
            .otherwise(pl.col("order") + len(symbols))
            .alias("order")
        )
        
        # 合并回去
        out = pl.concat([other_groups, this_group], how="diagonal_relaxed")
        out.write_parquet(p)
        return list_group_items(group_id)
    except pl.exceptions.PolarsError as e:
        logger.error(f"Parquet error reordering items: {e}")
        _handle_corrupted_file(p)
        raise ValueError("排序股票时出错，请重试")
    except OSError as e:
        logger.error(f"OS error reordering items: {e}")
        _handle_corrupted_file(p)
        raise ValueError("排序股票时出错，请重试")
    except Exception as e:
        logger.error(f"Unexpected error reordering items: {e}", exc_info=True)
        _handle_corrupted_file(p)
        raise ValueError("排序股票时出错，请重试")


def get_all_watchlist_symbols() -> list[str]:
    """获取所有板块中的所有股票（去重）。"""
    p = _items_path()
    if not p.exists():
        return []
    try:
        df = pl.read_parquet(p)
        if df.is_empty():
            return []
        return list(set(df["symbol"].to_list()))
    except pl.exceptions.PolarsError as e:
        logger.warning(f"Parquet error reading items file: {e}")
    except OSError as e:
        logger.warning(f"OS error reading items file: {e}")
    except Exception as e:
        logger.warning(f"Unexpected error reading items file: {e}", exc_info=True)
    return []
