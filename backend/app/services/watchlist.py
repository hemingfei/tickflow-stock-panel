"""自选股与分组服务。

自选存储于 ``data/user_data/watchlist.parquet``，分组定义与成员关系统一存于
``watchlist_group_store.json`` (与自选板块共用单一数据源, 见 watchlist_group_store)。

成员关系为多值 (M:N): 每条自选带 ``group_ids: list[str]``, 同一标的可同时
属于多个分组; 移出分组只摘标签(标的仍在自选), 移出自选才删除实体。
entries.parquet 中的 ``group_ids`` 列为派生缓存 —— 读取时以统一存储校正,
写入时按统一存储重算, 分组成员关系的权威数据在 watchlist_group_store。
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime
from pathlib import Path

import polars as pl

from app.config import settings
from app.services import watchlist_group_store as _group_store
from app.tickflow.capabilities import Cap, CapabilitySet
from app.tickflow.client import get_client
from app.tickflow.rate_limits import chunked, resolve_limit

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
# 数据版本号: 每次写盘 +1 (在 _LOCK 内递增, 读取免锁)。供监控引擎等进程内
# 消费方做缓存失效判断 —— 版本没变就不必重读文件, 版本一变立即拿到新成员。
_REVISION = 0


def revision() -> int:
    """自选/分组数据版本号, 每次写操作递增。"""
    return _REVISION
_MAX_GROUP_NAME_LENGTH = 24
DEFAULT_GROUP_COLOR = "sky"
GROUP_COLORS = frozenset({
    "sky",
    "blue",
    "indigo",
    "violet",
    "fuchsia",
    "rose",
    "orange",
    "amber",
    "lime",
    "emerald",
    "teal",
    "cyan",
})
_ENTRY_SCHEMA = {
    "symbol": pl.Utf8,
    "added_at": pl.Utf8,
    "note": pl.Utf8,
    "group_ids": pl.List(pl.Utf8),
}


def _path() -> Path:
    p = settings.data_dir / "user_data" / "watchlist.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _empty_entries() -> pl.DataFrame:
    return pl.DataFrame(schema=_ENTRY_SCHEMA)


def _sync_group_ids(df: pl.DataFrame) -> pl.DataFrame:
    """以统一存储的成员关系覆盖 group_ids 派生缓存列。"""
    if df.is_empty():
        return df
    mapping: dict[str, list[str]] = {}
    for m in _group_store.load()["members"]:
        mapping.setdefault(m["symbol"], []).append(m["group_id"])
    return df.with_columns(
        pl.Series(
            "group_ids",
            [mapping.get(s, []) for s in df["symbol"].to_list()],
            dtype=pl.List(pl.Utf8),
        )
    )


def _read_entries() -> pl.DataFrame:
    p = _path()
    if not p.exists():
        return _empty_entries()
    df = pl.read_parquet(p)
    # 旧 schema 兼容: 单值 group_id → group_ids=[gid]; 两列都缺 → 空列表
    if "group_ids" not in df.columns:
        old = df["group_id"].to_list() if "group_id" in df.columns else [None] * df.height
        df = df.with_columns(
            pl.Series("group_ids", [[g] if g else [] for g in old], dtype=pl.List(pl.Utf8))
        ).drop("group_id", strict=False)
    if "symbol" not in df.columns:
        df = df.with_columns(pl.lit("", dtype=pl.Utf8).alias("symbol"))
    if "added_at" not in df.columns:
        df = df.with_columns(pl.lit("", dtype=pl.Utf8).alias("added_at"))
    if "note" not in df.columns:
        df = df.with_columns(pl.lit(None, dtype=pl.Utf8).alias("note"))
    return _sync_group_ids(df.select(list(_ENTRY_SCHEMA)))


def _write_entries(df: pl.DataFrame) -> pl.DataFrame:
    """写盘自选条目; 返回按统一存储校正过 group_ids 的 DataFrame (供调用方返回)。"""
    global _REVISION
    p = _path()
    # 首次从旧 schema 迁移到 group_ids 前, 备份原文件(一次性)
    if p.exists():
        try:
            if "group_ids" not in pl.read_parquet_schema(p).names():
                shutil.copy(p, p.with_suffix(p.suffix + ".bak"))
        except OSError as e:
            logger.warning("watchlist backup before migration failed: %s", e)
    df = _sync_group_ids(df)
    tmp = p.with_suffix(p.suffix + ".tmp")
    df.select(list(_ENTRY_SCHEMA)).write_parquet(tmp)
    os.replace(tmp, p)
    _REVISION += 1
    return df


def _read_groups() -> list[dict]:
    """分组定义 (统一存储投影, 按 order 排序 —— 与板块视图共享同一顺序)。"""
    ordered = sorted(
        _group_store.load()["groups"], key=lambda g: (g["order"], g["id"])
    )
    return [
        {
            "id": g["id"],
            "name": g["name"],
            "color": g["color"] if g["color"] in GROUP_COLORS else DEFAULT_GROUP_COLOR,
        }
        for g in ordered
    ]


def _write_groups(groups: list[dict]) -> None:
    """按给定数组顺序写回分组定义 (order = 数组序, 保留既有 created_at)。"""
    global _REVISION
    data = _group_store.load()
    old = {g["id"]: g for g in data["groups"]}
    data["groups"] = [
        {
            "id": g["id"],
            "name": g["name"],
            "color": g["color"],
            "order": i,
            "created_at": old.get(g["id"], {}).get("created_at") or datetime.utcnow().isoformat(timespec="seconds"),
        }
        for i, g in enumerate(groups)
    ]
    _group_store.save(data)
    _REVISION += 1


def _normalize_group_name(name: str) -> str:
    normalized = name.strip()
    if not normalized:
        raise ValueError("分组名称不能为空")
    if len(normalized) > _MAX_GROUP_NAME_LENGTH:
        raise ValueError(f"分组名称不能超过 {_MAX_GROUP_NAME_LENGTH} 个字符")
    return normalized


def _normalize_group_color(color: str | None) -> str:
    normalized = (color or DEFAULT_GROUP_COLOR).strip().lower()
    if normalized not in GROUP_COLORS:
        raise ValueError("不支持的分组颜色")
    return normalized


def _validate_group_id(group_id: str | None, groups: list[dict]) -> None:
    if group_id is not None and not any(group["id"] == group_id for group in groups):
        raise ValueError("自选分组不存在")


def list_symbols() -> list[dict]:
    with _LOCK:
        df = _read_entries()
        return [] if df.is_empty() else df.to_dicts()


def add(symbol: str, note: str = "", group_id: str | None = None) -> list[dict]:
    rows, _ = add_batch([symbol], note=note, group_id=group_id)
    return rows


def add_batch(
    symbols: list[str],
    note: str = "",
    group_id: str | None = None,
    group_ids: list[str] | None = None,
) -> tuple[list[dict], int]:
    """批量添加并保持既有语义：每个新处理的标的移动到列表最前面。

    分组为可选的初始分组：``group_id`` 单组（如从某分组页添加）或 ``group_ids``
    多组（如批量导入同时并入多个分组）。重复添加的标的保留既有全部分组，
    仅把尚未属于的传入分组并入；二者可同时使用、内部去重。
    """
    with _LOCK:
        groups = _read_groups()
        # 合并单/多组参数并去重；逐组校验存在性
        apply_ids: list[str] = []
        for gid in (group_ids or []) + ([group_id] if group_id is not None else []):
            if gid in apply_ids:
                continue
            _validate_group_id(gid, groups)
            apply_ids.append(gid)
        rows = _read_entries().to_dicts()
        added = 0
        for symbol in symbols:
            existing = next((row for row in rows if row["symbol"] == symbol), None)
            if existing is None:
                added += 1
            rows = [row for row in rows if row["symbol"] != symbol]
            gids = list((existing or {}).get("group_ids") or [])
            for gid in apply_ids:
                if gid not in gids:
                    gids.append(gid)
            rows.insert(0, {
                "symbol": symbol,
                "added_at": datetime.utcnow().isoformat(timespec="seconds"),
                "note": note,
                "group_ids": gids,
            })
        # 成员关系并入统一存储 (apply_ids 中尚未属于的分组)
        if apply_ids:
            data = _group_store.load()
            for gid in apply_ids:
                owned = {m["symbol"] for m in data["members"] if m["group_id"] == gid}
                count = sum(1 for m in data["members"] if m["group_id"] == gid)
                for symbol in symbols:
                    if symbol in owned:
                        continue
                    data["members"].append({
                        "group_id": gid,
                        "symbol": symbol,
                        "order": count,
                        "note": "",
                        "added_at": datetime.utcnow().isoformat(timespec="seconds"),
                    })
                    count += 1
            _group_store.save(data)
        out = pl.DataFrame(rows, schema=_ENTRY_SCHEMA) if rows else _empty_entries()
        out = _write_entries(out)
        return out.to_dicts(), added


def remove(symbol: str) -> list[dict]:
    with _LOCK:
        df = _read_entries().filter(pl.col("symbol") != symbol)
        df = _write_entries(df)
        data = _group_store.load()
        kept = [m for m in data["members"] if m["symbol"] != symbol]
        if len(kept) != len(data["members"]):
            data["members"] = kept
            _group_store.save(data)
        return df.to_dicts()


def move_to_top(symbol: str) -> list[dict]:
    with _LOCK:
        df = _read_entries()
        if df.is_empty() or symbol not in df["symbol"].to_list():
            return df.to_dicts()
        target = df.filter(pl.col("symbol") == symbol)
        rest = df.filter(pl.col("symbol") != symbol)
        out = pl.concat([target, rest], how="diagonal_relaxed")
        out = _write_entries(out)
        return out.to_dicts()


def clear() -> int:
    """清空自选列表。返回移除的数量。"""
    with _LOCK:
        df = _read_entries()
        count = df.height
        if count > 0:
            _write_entries(_empty_entries())
            data = _group_store.load()
            data["members"] = []
            _group_store.save(data)
        return count


def list_groups() -> list[dict]:
    with _LOCK:
        return _read_groups()


def create_group(name: str, color: str | None = None) -> tuple[list[dict], dict]:
    with _LOCK:
        normalized = _normalize_group_name(name)
        normalized_color = _normalize_group_color(color)
        groups = _read_groups()
        if any(group["name"].casefold() == normalized.casefold() for group in groups):
            raise ValueError("分组名称已存在")
        group = {
            "id": uuid.uuid4().hex,
            "name": normalized,
            "color": normalized_color,
        }
        groups.append(group)
        _write_groups(groups)
        return groups, group


def rename_group(group_id: str, name: str, color: str | None = None) -> list[dict]:
    with _LOCK:
        normalized = _normalize_group_name(name)
        groups = _read_groups()
        target = next((group for group in groups if group["id"] == group_id), None)
        if target is None:
            raise KeyError(group_id)
        if any(
            group["id"] != group_id and group["name"].casefold() == normalized.casefold()
            for group in groups
        ):
            raise ValueError("分组名称已存在")
        target["name"] = normalized
        if color is not None:
            target["color"] = _normalize_group_color(color)
        _write_groups(groups)
        return groups


def reorder_groups(ordered_ids: list[str]) -> list[dict]:
    """按给定 id 顺序重排分组 (json 数组顺序即定义顺序)。"""
    with _LOCK:
        groups = _read_groups()
        by_id = {group["id"]: group for group in groups}
        if len(ordered_ids) != len(groups) or set(ordered_ids) != set(by_id):
            raise ValueError("分组顺序与现有分组不一致")
        reordered = [by_id[group_id] for group_id in ordered_ids]
        _write_groups(reordered)
        return reordered


def delete_group(group_id: str) -> tuple[list[dict], list[dict]]:
    """删除分组定义，原分组内的自选保留并转为未分组(仅摘掉该组标签)。"""
    with _LOCK:
        groups = _read_groups()
        if not any(group["id"] == group_id for group in groups):
            raise KeyError(group_id)
        data = _group_store.load()
        data["groups"] = [g for g in data["groups"] if g["id"] != group_id]
        data["members"] = [m for m in data["members"] if m["group_id"] != group_id]
        _group_store.save(data)
        df = _read_entries()
        df = _write_entries(df)
        remaining = [group for group in groups if group["id"] != group_id]
        return remaining, df.to_dicts()


def set_group(symbol: str, group_id: str | None) -> list[dict]:
    """互斥设定: 该标的只保留这一个分组(group_id=None 即全部移出, 变未分组)。

    多组模型的日常操作走 add_to_group / remove_from_group; 本函数服务于
    「仅保留此组」的显式场景。
    """
    with _LOCK:
        groups = _read_groups()
        _validate_group_id(group_id, groups)
        rows = _read_entries().to_dicts()
        if not any(row["symbol"] == symbol for row in rows):
            raise KeyError(symbol)
        data = _group_store.load()
        old_notes = {
            m["group_id"]: m["note"] for m in data["members"] if m["symbol"] == symbol
        }
        kept = [m for m in data["members"] if m["symbol"] != symbol]
        if group_id is not None:
            kept.append({
                "group_id": group_id,
                "symbol": symbol,
                "order": 0,
                "note": old_notes.get(group_id, ""),
                "added_at": datetime.utcnow().isoformat(timespec="seconds"),
            })
        data["members"] = kept
        _group_store.save(data)
        out = pl.DataFrame(rows, schema=_ENTRY_SCHEMA)
        out = _write_entries(out)
        return out.to_dicts()


def add_to_group(symbol: str, group_id: str) -> list[dict]:
    """把标的加入一个分组(多组成员关系: 不影响已属于的其他分组)。"""
    with _LOCK:
        groups = _read_groups()
        _validate_group_id(group_id, groups)
        rows = _read_entries().to_dicts()
        if not any(row["symbol"] == symbol for row in rows):
            raise KeyError(symbol)
        data = _group_store.load()
        if not any(
            m["group_id"] == group_id and m["symbol"] == symbol for m in data["members"]
        ):
            data["members"].append({
                "group_id": group_id,
                "symbol": symbol,
                "order": _group_store.group_member_count(data["members"], group_id),
                "note": "",
                "added_at": datetime.utcnow().isoformat(timespec="seconds"),
            })
            _group_store.save(data)
        out = pl.DataFrame(rows, schema=_ENTRY_SCHEMA)
        out = _write_entries(out)
        return out.to_dicts()


def remove_from_group(symbol: str, group_id: str) -> list[dict]:
    """把标的移出一个分组(仅摘本组标签; 标的仍在自选, 可能落入未分组)。"""
    with _LOCK:
        groups = _read_groups()
        _validate_group_id(group_id, groups)
        rows = _read_entries().to_dicts()
        if not any(row["symbol"] == symbol for row in rows):
            raise KeyError(symbol)
        data = _group_store.load()
        data["members"] = [
            m for m in data["members"]
            if not (m["group_id"] == group_id and m["symbol"] == symbol)
        ]
        _group_store.save(data)
        out = pl.DataFrame(rows, schema=_ENTRY_SCHEMA)
        out = _write_entries(out)
        return out.to_dicts()


def clear_group(group_id: str) -> list[dict]:
    """清空分组成员:把该分组标签从所有条目摘掉(变未分组),保留分组定义。"""
    with _LOCK:
        groups = _read_groups()
        if not any(group["id"] == group_id for group in groups):
            raise KeyError(group_id)
        data = _group_store.load()
        data["members"] = [m for m in data["members"] if m["group_id"] != group_id]
        _group_store.save(data)
        df = _read_entries()
        df = _write_entries(df)
        return df.to_dicts()


def fetch_quotes(symbols: list[str], capset: CapabilitySet, timeout_s: float = 8.0) -> list[dict]:
    """拉取实时行情。

    优先用 quote.batch;否则降级为 quote.by_symbol 单股请求。
    timeout_s: 单批次请求超时(秒)，防止 API 卡死阻塞整个请求。
    """
    if not symbols:
        return []

    tf = get_client()
    quotes: list[dict] = []

    # 走 batch
    if capset.has(Cap.QUOTE_BATCH):
        batch_size = resolve_limit(capset, Cap.QUOTE_BATCH, default_batch=50).batch
    elif capset.has(Cap.QUOTE_BY_SYMBOL):
        batch_size = resolve_limit(capset, Cap.QUOTE_BY_SYMBOL, default_batch=5).batch
    else:
        # 无任何实时行情能力(none/free 档走 free-api 服务器,不提供实时行情)
        # 提前返回空,避免发起注定失败的请求
        return []

    chunks = chunked(symbols, batch_size)

    # 用线程池为每个批次加超时保护
    pool = ThreadPoolExecutor(max_workers=1)
    for chunk in chunks:
        try:
            future = pool.submit(tf.quotes.get, symbols=chunk, as_dataframe=True)
            raw = future.result(timeout=timeout_s)
            if raw is None or len(raw) == 0:
                continue
            df = pl.from_pandas(raw)
            rename_map = {
                "last_price": "price",
                "ext.change_pct": "pct",
                "ext.name": "name",
            }
            df = df.rename({k: v for k, v in rename_map.items() if k in df.columns})
            quotes.extend(df.to_dicts())
        except FuturesTimeout:
            logger.warning("quote fetch timeout (%.1fs) for %d symbols", timeout_s, len(chunk))
            break  # 超时后不再尝试后续批次
        except Exception as e:  # noqa: BLE001
            logger.warning("quote fetch failed for %d symbols: %s", len(chunk), e)
    pool.shutdown(wait=False)

    return quotes
