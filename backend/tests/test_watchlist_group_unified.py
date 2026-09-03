"""自选分组统一存储: 旧数据迁移 + 自选分组/自选板块两视图互通。"""
import json

import polars as pl
import pytest

from app.config import settings
from app.services import watchlist, watchlist_group_store as store, watchlist_groups as boards


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store._cache = None
    store._cache_sig = None
    yield
    store._cache = None
    store._cache_sig = None


def test_migration_merges_legacy_sources(monkeypatch, tmp_path):
    """main 分组 json + entries.group_ids + 板块双 parquet → 合并进统一存储。"""
    user = tmp_path / "user_data"
    user.mkdir(parents=True)

    # main: 分组定义 (含与板块同名的"液冷" → 归并)
    (user / "watchlist_groups.json").write_text(json.dumps([
        {"id": "ma", "name": "-main组", "color": "rose"},
        {"id": "m-lq", "name": "液冷", "color": "sky"},
    ], ensure_ascii=False), encoding="utf-8")
    # main: 自选条目, ma 含两个标的
    pl.DataFrame({
        "symbol": ["600000.SH", "000001.SZ"],
        "added_at": ["2026-08-01"] * 2,
        "note": ["", ""],
        "group_ids": [["ma", "m-lq"], ["ma"]],
    }).write_parquet(user / "watchlist.parquet")
    # hmf 板块: 同名"液冷"(归并到 m-lq) + 独立板块"AI"
    pl.DataFrame({
        "group_id": ["b-lq", "b-ai"],
        "name": ["液冷", "AI"],
        "order": [1, 2],
        "created_at": ["2026-08-02", "2026-08-03"],
    }).write_parquet(user / "watchlist_groups.parquet")
    pl.DataFrame({
        "group_id": ["b-lq", "b-ai"],
        "symbol": ["300750.SZ", "002230.SZ"],
        "order": [1, 1],
        "note": ["板块备注", ""],
        "added_at": ["2026-08-02", "2026-08-03"],
    }).write_parquet(user / "watchlist_group_items.parquet")

    data = store.load()
    by_name = {g["name"]: g["id"] for g in data["groups"]}
    # 同名归并: 只有一个"液冷", 保留 main 的 id
    assert by_name["液冷"] == "m-lq"
    assert by_name["-main组"] == "ma"
    assert by_name["AI"] == "b-ai"
    assert len(data["groups"]) == 3

    members = {(m["group_id"], m["symbol"]): m for m in data["members"]}
    # main 条目成员并入
    assert ("ma", "600000.SH") in members
    assert ("m-lq", "600000.SH") in members
    assert ("ma", "000001.SZ") in members
    # 板块成员并入 (归并后的组 id)
    assert ("m-lq", "300750.SZ") in members
    assert members[("m-lq", "300750.SZ")]["note"] == "板块备注"
    assert ("b-ai", "002230.SZ") in members

    # 旧文件保留 (回滚兼容)
    assert (user / "watchlist_groups.json").exists()
    assert (user / "watchlist_groups.parquet").exists()


def test_two_views_share_group_definitions(monkeypatch, tmp_path):
    """main 创建的分组在板块视图可见, 反之亦然; 重命名双向同步。"""
    groups, created = watchlist.create_group("互通组", "violet")
    assert [g["name"] for g in boards.list_groups()] == ["互通组"]

    created_list = boards.create_group("板块组")
    created_b = next(g["group_id"] for g in created_list if g["name"] == "板块组")
    assert [g["name"] for g in watchlist.list_groups()] == ["互通组", "板块组"]

    watchlist.rename_group(created["id"], "改名组")
    names = [g["name"] for g in boards.list_groups()]
    assert "改名组" in names and "互通组" not in names

    boards.rename_group(created_b, "板块改名")
    names = [g["name"] for g in watchlist.list_groups()]
    assert "板块改名" in names

    assert groups[-1]["name"] == "互通组"  # main 视图投影不带板块元数据


def test_membership_flows_across_views(monkeypatch, tmp_path):
    """任一视图的成员变更在另一视图可见。"""
    _, created = watchlist.create_group("共享组")
    gid = created["id"]

    # main 加入自选并设组 → 板块 items 可见
    watchlist.add("600000.SH", group_id=gid)
    items = boards.list_group_items(gid)
    assert [i["symbol"] for i in items] == ["600000.SH"]

    # 板块加入成员 → 自动进入自选主列表, 且条目 group_ids 同步
    boards.add_item(gid, "300750.SZ", note="板块来")
    rows = {r["symbol"]: r for r in watchlist.list_symbols()}
    assert rows["300750.SZ"]["group_ids"] == [gid]
    assert rows["600000.SH"]["group_ids"] == [gid]
    assert rows["300750.SZ"]["note"] == ""  # 板块组内备注不写自选条目备注

    # 板块删除成员 → main 条目 group_ids 摘除
    boards.remove_item(gid, "300750.SZ")
    rows = {r["symbol"]: r for r in watchlist.list_symbols()}
    assert rows["300750.SZ"]["group_ids"] == []

    # 板块删除板块 → main 分组消失, 标的仍在自选
    boards.delete_group(gid)
    assert watchlist.list_groups() == []
    assert {r["symbol"] for r in watchlist.list_symbols()} == {"600000.SH", "300750.SZ"}
    assert all(r["group_ids"] == [] for r in watchlist.list_symbols())


def test_board_reorder_syncs_to_main_view(monkeypatch, tmp_path):
    """板块排序与自选分组定义顺序是同一字段。"""
    ga = watchlist.create_group("A")[1]
    gb = watchlist.create_group("B")[1]
    boards.reorder_groups([gb["id"], ga["id"]])
    assert [g["name"] for g in watchlist.list_groups()] == ["B", "A"]


def test_board_name_collides_with_main_group(monkeypatch, tmp_path):
    """分组实体统一后, 名称唯一性跨视图生效。"""
    watchlist.create_group("重名组")
    with pytest.raises(ValueError, match="已存在"):
        boards.create_group("重名组")


def test_ensure_symbols_appends_missing_preserving_order(monkeypatch, tmp_path):
    """ensure_symbols: 缺失的按给定顺序追加到末尾, 已存在的不动。"""
    watchlist.add("600000.SH")
    watchlist.add("000001.SZ")
    added = watchlist.ensure_symbols(["000001.SZ", "300750.SZ", "688981.SH", ""])
    assert added == 2
    # add 是插到最前: 已有顺序 [000001.SZ, 600000.SH]; 缺失的按给定顺序追加到末尾
    rows = watchlist.list_symbols()
    assert [r["symbol"] for r in rows] == ["000001.SZ", "600000.SH", "300750.SZ", "688981.SH"]


def test_board_import_adds_members_into_watchlist(monkeypatch, tmp_path):
    """板块导入后, 成员股票自动出现在自选主列表并带上分组归属。"""
    config = {"version": 1, "groups": [
        {"name": "导入组A", "items": [
            {"symbol": "300750.SZ", "note": "", "order": 1, "added_at": ""},
            {"symbol": "002463.SZ", "note": "", "order": 2, "added_at": ""},
        ]},
        {"name": "导入组B", "items": [
            {"symbol": "688981.SH", "note": "", "order": 1, "added_at": ""},
        ]},
    ], "settings": {}}

    boards.import_config(config, replace=True)

    rows = {r["symbol"]: r for r in watchlist.list_symbols()}
    assert set(rows) == {"300750.SZ", "002463.SZ", "688981.SH"}
    by_name = {g["name"]: g["id"] for g in watchlist.list_groups()}
    assert rows["300750.SZ"]["group_ids"] == [by_name["导入组A"]]
    assert rows["688981.SH"]["group_ids"] == [by_name["导入组B"]]

    # 板块删除成员只摘分组标签, 自选保留 (对齐自选分组语义)
    gid_a = by_name["导入组A"]
    boards.remove_item(gid_a, "300750.SZ")
    rows = {r["symbol"]: r for r in watchlist.list_symbols()}
    assert "300750.SZ" in rows and rows["300750.SZ"]["group_ids"] == []
