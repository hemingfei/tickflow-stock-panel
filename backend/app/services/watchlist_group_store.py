"""自选分组统一存储 —— main 自选分组与 hmf 自选板块的单一数据源。

存储: ``data/user_data/watchlist_groups_store.json``

    {
      "version": 1,
      "groups":  [{"id", "name", "color", "order", "created_at"}],
      "members": [{"group_id", "symbol", "order", "note", "added_at"}]
    }

- ``groups.order``: 分组定义顺序 (自选分组标签栏顺序 = 板块侧栏顺序, 同一来源)
- ``members``: ``(group_id, symbol)`` 唯一; ``order`` 为组内顺序, ``note`` 为
  组×标的 级备注 (自选条目本身的备注仍存 watchlist.parquet.note, 与此无关)

两个视图模块各自适配本存储 (API 契约不变):
- ``app.services.watchlist``: 分组 = {id,name,color}; 成员关系表现为自选条目的
  ``group_ids`` (entries.parquet 中的 group_ids 列仅为派生缓存, 读取时以本存储校正)
- ``app.services.watchlist_groups`` (板块): 分组 = {group_id,name,order,created_at};
  成员 = 组内 items (含组内 note / order)

首次访问自动迁移旧数据 (同名分组按 casefold 归并为同一分组, 成员取并集):
- ``watchlist_groups.json`` (main 分组定义) + ``watchlist.parquet`` 的 group_ids 列
- ``watchlist_groups.parquet`` / ``watchlist_group_items.parquet`` (hmf 板块)
旧文件保留在原地不再读写 —— 回滚到旧版本代码时可继续使用。
"""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

# 可重入: 视图模块持有自身锁的写路径内会再调 load/save
LOCK = threading.RLock()

_VERSION = 1
DEFAULT_GROUP_COLOR = "sky"

_cache: dict | None = None
_cache_sig: tuple[int, int] | None = None


def _store_path() -> Path:
    p = settings.data_dir / "user_data" / "watchlist_groups_store.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _empty() -> dict:
    return {"version": _VERSION, "groups": [], "members": []}


def _normalize(data: object) -> dict:
    """清洗为合法结构: 过滤无效行, (group_id, symbol) 去重, 组内 order 重排为连续。"""
    out = _empty()
    if not isinstance(data, dict):
        return out

    seen_groups: set[str] = set()
    groups = data.get("groups")
    if isinstance(groups, list):
        for i, g in enumerate(groups):
            if not isinstance(g, dict):
                continue
            gid = str(g.get("id") or "").strip()
            name = str(g.get("name") or "").strip()
            if not gid or not name or gid in seen_groups:
                continue
            seen_groups.add(gid)
            try:
                order = int(g.get("order", i))
            except (TypeError, ValueError):
                order = i
            out["groups"].append({
                "id": gid,
                "name": name,
                "color": str(g.get("color") or DEFAULT_GROUP_COLOR),
                "order": order,
                "created_at": str(g.get("created_at") or ""),
            })

    members = data.get("members")
    if isinstance(members, list):
        seen_pairs: set[tuple[str, str]] = set()
        for m in members:
            if not isinstance(m, dict):
                continue
            gid = str(m.get("group_id") or "").strip()
            sym = str(m.get("symbol") or "").strip().upper()
            if not gid or not sym or gid not in seen_groups or (gid, sym) in seen_pairs:
                continue
            seen_pairs.add((gid, sym))
            try:
                order = int(m.get("order", 0))
            except (TypeError, ValueError):
                order = 0
            out["members"].append({
                "group_id": gid,
                "symbol": sym,
                "order": order,
                "note": str(m.get("note") or ""),
                "added_at": str(m.get("added_at") or ""),
            })

    # 组内 order 规整为 0..n-1 (稀疏/重复 order 由排序消化)
    by_group: dict[str, int] = {}
    for m in sorted(out["members"], key=lambda m: (m["group_id"], m["order"])):
        m["order"] = by_group.get(m["group_id"], 0)
        by_group[m["group_id"]] = by_group.get(m["group_id"], 0) + 1
    return out


def _read_legacy_json(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning("legacy %s read failed, skipped: %s", path.name, e)
        return []
    return raw if isinstance(raw, list) else []


def _read_legacy_parquet(path: Path, columns: list[str]) -> list[dict]:
    """读取旧文件指定列 (任一列存在即可, 缺失的列填 None —— 兼容旧 schema)。"""
    try:
        import polars as pl

        if not path.exists():
            return []
        df = pl.read_parquet(path)
        if df.is_empty() or not any(col in df.columns for col in columns):
            return []
        for col in columns:
            if col not in df.columns:
                df = df.with_columns(pl.lit(None).alias(col))
        return df.select(columns).to_dicts()
    except Exception as e:  # noqa: BLE001
        logger.warning("legacy %s read failed, skipped: %s", path.name, e)
        return []


def _migrate_from_legacy() -> dict:
    """合并旧存储 (main 分组 json + entries.group_ids + hmf 板块双 parquet)。"""
    data = _empty()
    user_dir = settings.data_dir / "user_data"

    # 1) main 分组定义: [{id,name,color}], 数组顺序即顺序
    legacy_json = user_dir / "watchlist_groups.json"
    for i, g in enumerate(_read_legacy_json(legacy_json)):
        if not isinstance(g, dict):
            continue
        gid = str(g.get("id") or "").strip()
        name = str(g.get("name") or "").strip()
        if not gid or any(x["name"].casefold() == name.casefold() for x in data["groups"]):
            continue
        data["groups"].append({
            "id": gid,
            "name": name,
            "color": str(g.get("color") or DEFAULT_GROUP_COLOR),
            "order": i,
            "created_at": _now(),
        })

    # 2) main 自选条目的成员关系 (group_ids M:N 列; 旧 schema 为单值 group_id 列)
    entries_rows = _read_legacy_parquet(
        user_dir / "watchlist.parquet", ["symbol", "group_ids", "group_id"])
    per_group: dict[str, int] = {}
    for row in entries_rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        gids = row.get("group_ids")
        if gids is None:
            gids = [row.get("group_id")]  # 旧 schema 单值
        if isinstance(gids, str):
            gids = [gids]
        for gid in gids:
            gid = str(gid or "").strip()
            if not symbol or not gid:
                continue
            if not any(g["id"] == gid for g in data["groups"]):
                continue  # 指向已不存在分组的脏引用
            if any(m["group_id"] == gid and m["symbol"] == symbol for m in data["members"]):
                continue
            data["members"].append({
                "group_id": gid,
                "symbol": symbol,
                "order": per_group.get(gid, 0),
                "note": "",
                "added_at": "",
            })
            per_group[gid] = per_group.get(gid, 0) + 1

    # 3) hmf 板块定义 (同名归并到 main 分组; 不同名保留原 group_id)
    hmf_groups = _read_legacy_parquet(user_dir / "watchlist_groups.parquet", ["group_id", "name", "order"])
    id_map: dict[str, str] = {}
    for row in sorted(hmf_groups, key=lambda r: r.get("order") if isinstance(r.get("order"), int) else 0):
        gid = str(row.get("group_id") or "").strip()
        name = str(row.get("name") or "").strip()
        if not gid or not name:
            continue
        dup = next((g for g in data["groups"] if g["name"].casefold() == name.casefold()), None)
        if dup is not None:
            id_map[gid] = dup["id"]
            continue
        new_id = gid if not any(g["id"] == gid for g in data["groups"]) else f"migrated-{gid}"
        id_map[gid] = new_id
        data["groups"].append({
            "id": new_id,
            "name": name,
            "color": DEFAULT_GROUP_COLOR,
            "order": len(data["groups"]),
            "created_at": str(row.get("created_at") or _now()),
        })

    # 4) hmf 板块成员 (note 为组×标的级, 并入时仅补空缺)
    hmf_items = _read_legacy_parquet(
        user_dir / "watchlist_group_items.parquet", ["group_id", "symbol", "note"])
    for row in hmf_items:
        gid = id_map.get(str(row.get("group_id") or "").strip())
        symbol = str(row.get("symbol") or "").strip().upper()
        if not gid or not symbol:
            continue
        existing = next(
            (m for m in data["members"] if m["group_id"] == gid and m["symbol"] == symbol), None)
        note = str(row.get("note") or "")
        if existing is not None:
            if not existing["note"] and note:
                existing["note"] = note
            continue
        data["members"].append({
            "group_id": gid,
            "symbol": symbol,
            "order": per_group.get(gid, 0),
            "note": note,
            "added_at": str(row.get("added_at") or ""),
        })
        per_group[gid] = per_group.get(gid, 0) + 1

    return data


def _write_disk(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load() -> dict:
    """读取统一存储 (带 mtime 签名缓存)。返回深拷贝, 首次访问触发旧数据迁移。"""
    global _cache, _cache_sig
    with LOCK:
        p = _store_path()
        if not p.exists():
            data = _normalize(_migrate_from_legacy())
            _write_disk(p, data)
            _cache = data
            _cache_sig = (p.stat().st_mtime_ns, p.stat().st_size)
            logger.info(
                "watchlist group store initialized: %d groups, %d members",
                len(data["groups"]), len(data["members"]),
            )
            return copy.deepcopy(data)
        try:
            sig = (p.stat().st_mtime_ns, p.stat().st_size)
        except OSError:
            return copy.deepcopy(_cache) if _cache is not None else _empty()
        if _cache is not None and sig == _cache_sig:
            return copy.deepcopy(_cache)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning("watchlist group store malformed: %s", e)
            try:
                bak = p.parent / f"watchlist_groups_store.json.bak.{int(datetime.now().timestamp())}"
                p.rename(bak)
                logger.info("backed up corrupted group store to %s", bak)
            except OSError:
                pass
            data = _empty()
        data = _normalize(data)
        _cache = data
        _cache_sig = sig
        return copy.deepcopy(data)


def save(data: dict) -> None:
    """整体写入统一存储 (规范化 + 原子写 + 失效缓存)。"""
    global _cache, _cache_sig
    with LOCK:
        clean = _normalize(data)
        p = _store_path()
        _write_disk(p, clean)
        _cache = clean
        _cache_sig = (p.stat().st_mtime_ns, p.stat().st_size)


def member_group_ids(members: list[dict], symbol: str) -> list[str]:
    """某标的当前所属分组 id 列表 (按 members 数组序)。"""
    return [m["group_id"] for m in members if m["symbol"] == symbol]


def group_member_count(members: list[dict], group_id: str) -> int:
    return sum(1 for m in members if m["group_id"] == group_id)
