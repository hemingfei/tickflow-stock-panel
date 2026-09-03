"""自选板块 API 服务 (独立页面的板块视图)。

数据与自选分组共用统一存储 ``watchlist_groups_store.json``
(见 watchlist_group_store) —— 同一组分组定义与成员关系, 本模块提供
板块视角的读写: 分组 = {group_id, name, order, created_at},
成员 = 组内 items (含组内 order / note)。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime

from app.services import watchlist_group_store as store
from app.services import watchlist as _watchlist_entries

logger = logging.getLogger(__name__)

_MAX_ITEM_NOTE_LENGTH = 200


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _view(g: dict) -> dict:
    """统一存储分组 → 板块视图 ({group_id, name, order, created_at})。"""
    return {
        "group_id": g["id"],
        "name": g["name"],
        "order": g["order"],
        "created_at": g["created_at"],
    }


def _sorted_groups(data: dict) -> list[dict]:
    return sorted(
        (_view(g) for g in data["groups"]), key=lambda g: (g["order"], g["group_id"])
    )


def list_groups() -> list[dict]:
    """列出所有板块，按 order 排序。"""
    return _sorted_groups(store.load())


def _get_group_by_name(name: str) -> dict | None:
    target = name.strip().casefold()
    for g in store.load()["groups"]:
        if g["name"].casefold() == target:
            return _view(g)
    return None


def _get_group_by_id(group_id: str) -> dict | None:
    for g in store.load()["groups"]:
        if g["id"] == group_id:
            return _view(g)
    return None


def create_group(name: str) -> list[dict]:
    """创建板块。"""
    name = name.strip()
    if not name:
        raise ValueError("板块名称不能为空")

    data = store.load()
    if any(g["name"].casefold() == name.casefold() for g in data["groups"]):
        raise ValueError("板块名称已存在")

    next_order = max((g["order"] for g in data["groups"]), default=0) + 1
    data["groups"].append({
        "id": str(uuid.uuid4()),
        "name": name,
        "color": store.DEFAULT_GROUP_COLOR,
        "order": next_order,
        "created_at": _now(),
    })
    store.save(data)
    return list_groups()


def rename_group(group_id: str, new_name: str) -> list[dict]:
    """重命名板块。"""
    group_id = group_id.strip()
    new_name = new_name.strip()

    if not new_name:
        raise ValueError("板块名称不能为空")

    data = store.load()
    target = next((g for g in data["groups"] if g["id"] == group_id), None)
    if target is None:
        raise ValueError("板块不存在")
    if any(
        g["id"] != group_id and g["name"].casefold() == new_name.casefold()
        for g in data["groups"]
    ):
        raise ValueError("板块名称已存在")

    target["name"] = new_name
    store.save(data)
    return list_groups()


def delete_group(group_id: str) -> list[dict]:
    """删除板块并级联删除关联股票。"""
    group_id = group_id.strip()

    data = store.load()
    if not any(g["id"] == group_id for g in data["groups"]):
        raise ValueError("板块不存在")
    data["groups"] = [g for g in data["groups"] if g["id"] != group_id]
    data["members"] = [m for m in data["members"] if m["group_id"] != group_id]
    store.save(data)
    return list_groups()


def reorder_groups(group_ids: list[str]) -> list[dict]:
    """重新排序板块。传入的板块按给定顺序排在前面, 未传入的保持相对顺序排后面。"""
    group_ids = [gid.strip() for gid in group_ids]
    order_map = {gid: i for i, gid in enumerate(group_ids)}

    data = store.load()
    passed = sorted(
        (g for g in data["groups"] if g["id"] in order_map),
        key=lambda g: order_map[g["id"]],
    )
    rest = [g for g in data["groups"] if g["id"] not in order_map]
    for i, g in enumerate(passed + rest):
        g["order"] = i
    store.save(data)
    return list_groups()


def get_groups_with_stats() -> list[dict]:
    """获取所有板块，包含 item_count 统计。"""
    data = store.load()
    counts: dict[str, int] = {}
    for m in data["members"]:
        counts[m["group_id"]] = counts.get(m["group_id"], 0) + 1
    result = []
    for g in _sorted_groups(data):
        g_copy = g.copy()
        g_copy["item_count"] = counts.get(g["group_id"], 0)
        result.append(g_copy)
    return result


def list_group_items(group_id: str) -> list[dict]:
    """列出板块内的股票，按 order 排序。"""
    group_id = group_id.strip()
    data = store.load()
    items = [
        {
            "group_id": m["group_id"],
            "symbol": m["symbol"],
            "order": m["order"],
            "note": m["note"],
            "added_at": m["added_at"],
        }
        for m in data["members"] if m["group_id"] == group_id
    ]
    return sorted(items, key=lambda m: (m["order"], m["symbol"]))


def add_item(group_id: str, symbol: str, note: str = "") -> list[dict]:
    """向板块添加股票 (已存在则更新备注并移到末尾)。"""
    group_id = group_id.strip()
    symbol = symbol.strip().upper()

    if not symbol:
        raise ValueError("股票代码不能为空")

    data = store.load()
    if not any(g["id"] == group_id for g in data["groups"]):
        raise ValueError("板块不存在")

    # 已存在则先移除 (保持原语义: 重复添加 = 移到末尾 + 更新备注)
    data["members"] = [
        m for m in data["members"]
        if not (m["group_id"] == group_id and m["symbol"] == symbol)
    ]
    data["members"].append({
        "group_id": group_id,
        "symbol": symbol,
        "order": store.group_member_count(data["members"], group_id) + 1,
        "note": note,
        "added_at": _now(),
    })
    store.save(data)
    # 分组成员即自选成员: 标的不在自选主列表时自动补入
    _watchlist_entries.ensure_symbols([symbol])
    return list_group_items(group_id)


def add_items_batch(group_id: str, symbols: list[str], note: str = "") -> tuple[list[dict], int]:
    """批量向板块添加股票 (已存在的跳过)。返回 (items, added_count)。"""
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

    data = store.load()
    existing = {m["symbol"] for m in data["members"] if m["group_id"] == group_id}
    to_add = [s for s in cleaned_symbols if s not in existing]
    added_count = len(to_add)
    if added_count > 0:
        base_order = store.group_member_count(data["members"], group_id) + 1
        for i, sym in enumerate(to_add):
            data["members"].append({
                "group_id": group_id,
                "symbol": sym,
                "order": base_order + i,
                "note": note,
                "added_at": _now(),
            })
        store.save(data)
        # 分组成员即自选成员: 标的不在自选主列表时自动补入
        _watchlist_entries.ensure_symbols(to_add)

    return list_group_items(group_id), added_count


def remove_item(group_id: str, symbol: str) -> list[dict]:
    """从板块移除股票。"""
    group_id = group_id.strip()
    symbol = symbol.strip().upper()

    data = store.load()
    data["members"] = [
        m for m in data["members"]
        if not (m["group_id"] == group_id and m["symbol"] == symbol)
    ]
    store.save(data)
    return list_group_items(group_id)


def reorder_items(group_id: str, symbols: list[str]) -> list[dict]:
    """重新排序板块内的股票。传入的按给定顺序排前面, 未传入的保持相对顺序排后面。"""
    group_id = group_id.strip()
    symbols = [s.strip().upper() for s in symbols]
    order_map = {sym: i for i, sym in enumerate(symbols)}

    data = store.load()
    this_group = sorted(
        (m for m in data["members"] if m["group_id"] == group_id),
        key=lambda m: (order_map.get(m["symbol"], len(symbols)), m["order"]),
    )
    for i, m in enumerate(this_group):
        m["order"] = i
    store.save(data)
    return list_group_items(group_id)


def get_all_watchlist_symbols() -> list[str]:
    """获取所有板块中的所有股票（去重）。"""
    return sorted({m["symbol"] for m in store.load()["members"]})


def export_config() -> dict:
    """导出完整的自选板块配置。

    返回包含分组、股票项和设置的完整字典。
    """
    from datetime import datetime
    from app.services import preferences

    # 获取所有分组
    groups = list_groups()

    # 获取每个分组的股票项
    groups_with_items = []
    for group in groups:
        items = list_group_items(group["group_id"])
        # 移除 group_id，只保留需要的字段
        cleaned_items = []
        for item in items:
            cleaned_item = {k: v for k, v in item.items() if k != "group_id"}
            cleaned_items.append(cleaned_item)
        # 清理分组字段，只保留必要的
        cleaned_group = {
            "name": group["name"],
            "order": group["order"],
            "created_at": group.get("created_at"),
            "items": cleaned_items
        }
        groups_with_items.append(cleaned_group)

    # 获取设置
    settings = {
        "view_mode": preferences.get_watchlist_groups_view_mode(),
        "display_style": preferences.get_watchlist_groups_display_style(),
        "avg_pct_mode": preferences.get_watchlist_groups_avg_pct_mode(),
        "columns": preferences.get_watchlist_groups_columns()
    }

    return {
        "version": 1,
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "groups": groups_with_items,
        "settings": settings
    }


def import_config(data: dict, replace: bool = False) -> list[dict]:
    """导入自选板块配置。

    Args:
        data: 导出的配置数据
        replace: 是否完全替换现有配置（True=替换，False=合并）
            替换模式会清空统一存储中的全部分组与成员 (分组实体已与
            自选分组共用, 两视图同步生效)。

    Returns:
        更新后的分组列表
    """
    from app.services import preferences

    # 验证数据格式
    if not isinstance(data, dict):
        raise ValueError("Invalid config data format")

    version = data.get("version", 1)
    if version != 1:
        raise ValueError(f"Unsupported config version: {version}")

    groups_data = data.get("groups", [])
    if not isinstance(groups_data, list):
        raise ValueError("Invalid groups data format")

    settings_data = data.get("settings", {})

    if replace:
        # 完全替换模式：先清空统一存储中的全部分组与成员
        data_all = store.load()
        data_all["groups"] = []
        data_all["members"] = []
        store.save(data_all)

    # 处理分组和股票项
    existing_groups = list_groups()
    existing_names = {g["name"].strip().lower(): g for g in existing_groups}

    new_groups = []
    for group_data in groups_data:
        group_name = str(group_data.get("name", "")).strip()
        if not group_name:
            continue

        items_data = group_data.get("items", [])
        if not isinstance(items_data, list):
            continue

        if replace or group_name.lower() not in existing_names:
            # 创建新分组
            new_group = create_group(group_name)
            if not new_group:
                continue
            # 获取刚创建的分组
            created_group = _get_group_by_name(group_name)
            if not created_group:
                continue
            group_id = created_group["group_id"]
        else:
            # 使用现有分组
            group_id = existing_names[group_name.lower()]["group_id"]

        # 添加股票到分组
        for item_data in items_data:
            symbol = str(item_data.get("symbol", "")).strip().upper()
            if not symbol:
                continue
            note = str(item_data.get("note", "")).strip()
            try:
                add_item(group_id, symbol, note)
            except Exception as e:
                logger.warning("Failed to add item %s to group %s: %s", symbol, group_id, e)

        new_groups.append(group_id)

    # 应用设置（如果有）
    if settings_data and isinstance(settings_data, dict):
        if "view_mode" in settings_data:
            try:
                preferences.set_watchlist_groups_view_mode(settings_data["view_mode"])
            except Exception as e:
                logger.warning("Failed to import view_mode setting: %s", e)
        if "display_style" in settings_data:
            try:
                preferences.set_watchlist_groups_display_style(settings_data["display_style"])
            except Exception as e:
                logger.warning("Failed to import display_style setting: %s", e)
        if "avg_pct_mode" in settings_data:
            try:
                preferences.set_watchlist_groups_avg_pct_mode(settings_data["avg_pct_mode"])
            except Exception as e:
                logger.warning("Failed to import avg_pct_mode setting: %s", e)
        if "columns" in settings_data:
            try:
                columns = settings_data["columns"]
                if columns is None or isinstance(columns, list):
                    preferences.set_watchlist_groups_columns(columns)
            except Exception as e:
                logger.warning("Failed to import columns setting: %s", e)

    return get_groups_with_stats()
