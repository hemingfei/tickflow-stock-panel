"""指数共振: 配置存储、窗口涨幅口径、共振评估与 API 契约。"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api import resonance as resonance_api
from app.config import settings
from app.market_time import CN_TZ
from app.services import index_resonance
from app.services.index_resonance import (
    IndexResonanceService,
    in_time_ranges,
    normalize_monitor,
    volume_ratio,
    window_change,
)


def _isolated_store(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(index_resonance, "_store_cache", None)
    monkeypatch.setattr(index_resonance, "_store_sig", None)
    return tmp_path


def _stub_groups(monkeypatch, groups: list[dict], members: list[dict]) -> None:
    monkeypatch.setattr(
        index_resonance.watchlist_group_store,
        "load",
        lambda: {"version": 1, "groups": groups, "members": members},
    )


def _config(**overrides) -> dict:
    config = {
        "id": "res_test",
        "name": "创业板共振",
        "index_symbol": "399006.SZ",
        "enabled": True,
        "time_ranges": [],
        "window_seconds": 300,
        "index_threshold_pct": 0.3,
        "group_up_ratio": 0.6,
        "min_group_members": 2,
        "group_ids": ["g1", "g2"],
        "created_at": "2026-09-12T00:00:00",
    }
    config.update(overrides)
    return config


def _repo():
    return SimpleNamespace(
        get_name_map=lambda symbols=None: {
            "600001.SH": "龙头一", "600002.SH": "跟风二",
        },
    )


def _service(monkeypatch, tmp_path, groups=None, members=None) -> IndexResonanceService:
    _isolated_store(monkeypatch, tmp_path)
    _stub_groups(
        monkeypatch,
        groups or [{"id": "g1", "name": "算力"}, {"id": "g2", "name": "医药"}],
        members or [
            {"group_id": "g1", "symbol": "600001.SH"},
            {"group_id": "g1", "symbol": "600002.SH"},
            {"group_id": "g2", "symbol": "600003.SH"},
            {"group_id": "g2", "symbol": "600004.SH"},
        ],
    )
    return IndexResonanceService(_repo())


def _stock_df(changes: dict[str, float]) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": list(changes.keys()),
        "change_pct": list(changes.values()),
    })


def _stock_df_v(changes: dict[str, float], volumes: dict[str, float]) -> pl.DataFrame:
    symbols = list(changes.keys())
    return pl.DataFrame({
        "symbol": symbols,
        "change_pct": [changes[s] for s in symbols],
        "volume": [volumes.get(s) for s in symbols],
    })


def _index_df(
    symbol: str, change_pct_percent: float, price: float = 2000.0,
    volume: float | None = None,
) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [symbol],
        "name": ["创业板指"],
        "last_price": [price],
        "change_pct": [change_pct_percent],
        "volume": [volume],
    })


# ── 配置规范化 ────────────────────────────────────────────────

def test_normalize_monitor_rejects_invalid_index_and_name():
    assert normalize_monitor({"id": "a", "name": "", "index_symbol": "399006.SZ"}) is None
    assert normalize_monitor({"id": "a", "name": "x", "index_symbol": "999999.SZ"}) is None
    assert normalize_monitor("not-a-dict") is None


def test_normalize_monitor_clamps_and_drops_bad_ranges():
    monitor = normalize_monitor({
        "id": "a",
        "name": "x",
        "index_symbol": "000001.SH",
        "time_ranges": [
            {"start": "09:30", "end": "10:30"},
            {"start": "11:00", "end": "10:00"},  # start >= end 丢弃
            {"start": "bad", "end": "11:00"},    # 非法丢弃
            {"start": "13:00", "end": "14:00"},
            {"start": "13:00", "end": "14:00"},  # 重复丢弃
        ],
        "window_minutes": 999,
        "index_threshold_pct": 99, "group_up_ratio": 2,
        "min_group_members": 1,
        "group_ids": ["g1", "", "g1"],
        "webhook_channels": ["feishu", "bogus", "", "EMAIL"],
        "notify_cooldown_seconds": 999999,
    })
    assert monitor is not None
    assert monitor["time_ranges"] == [
        {"start": 570, "end": 630}, {"start": 780, "end": 840},
    ]
    # 旧契约 window_minutes(分钟) 迁移为秒; 999 分钟超界 -> 回落默认 300s
    assert monitor["window_seconds"] == 300
    assert monitor["index_threshold_pct"] == 0.3
    assert monitor["group_up_ratio"] == 0.6
    assert monitor["min_group_members"] == 3
    assert monitor["group_ids"] == ["g1"]
    assert monitor["webhook_channels"] == ["feishu", "email"]
    assert monitor["notify_cooldown_seconds"] == 86400


def test_normalize_window_seconds_and_legacy_migration():
    # 新契约: 秒, 30..3600
    assert normalize_monitor({
        "id": "a", "name": "x", "index_symbol": "399006.SZ", "window_seconds": 30,
    })["window_seconds"] == 30
    assert normalize_monitor({
        "id": "a", "name": "x", "index_symbol": "399006.SZ", "window_seconds": 20,
    })["window_seconds"] == 300
    # 旧契约: 5 分钟 -> 300 秒
    assert normalize_monitor({
        "id": "a", "name": "x", "index_symbol": "399006.SZ", "window_minutes": 5,
    })["window_seconds"] == 300
    # 缺省 -> 300 秒
    assert normalize_monitor({
        "id": "a", "name": "x", "index_symbol": "399006.SZ",
    })["window_seconds"] == 300


def test_monitor_store_crud_roundtrip(monkeypatch, tmp_path):
    service = _service(monkeypatch, tmp_path)

    monitors = service.create_monitor({
        "name": "  创业板共振 ", "index_symbol": "399006.SZ",
        "index_threshold_pct": 0.5,
    })
    assert len(monitors) == 1
    created = monitors[0]
    assert created["name"] == "创业板共振"
    assert created["id"].startswith("res_")
    assert created["index_threshold_pct"] == 0.5
    assert created["enabled"] is True

    monitors = service.update_monitor(created["id"], {
        "name": "改名", "index_symbol": "399006.SZ", "enabled": False,
    })
    assert monitors[0]["name"] == "改名"
    assert monitors[0]["enabled"] is False
    assert service.has_enabled_monitors() is False

    with pytest.raises(KeyError):
        service.update_monitor("missing", {"name": "x", "index_symbol": "399006.SZ"})

    monitors = service.delete_monitor(created["id"])
    assert monitors == []
    with pytest.raises(KeyError):
        service.delete_monitor(created["id"])

    # 持久化可重读 (缓存失效后从磁盘加载)
    service.create_monitor({"name": "再建", "index_symbol": "000001.SH"})
    index_resonance._store_cache = None
    assert [m["name"] for m in service.list_monitors()] == ["再建"]


# ── 时间段与窗口涨幅 ──────────────────────────────────────────

def test_in_time_ranges_empty_means_full_session():
    now = datetime(2026, 9, 10, 14, 0, tzinfo=CN_TZ)
    assert in_time_ranges(now, []) is True
    ranges = [{"start": 9 * 60 + 30, "end": 10 * 60 + 30}]
    assert in_time_ranges(now, ranges) is False
    assert in_time_ranges(now.replace(hour=9, minute=30), ranges) is True
    assert in_time_ranges(now.replace(hour=10, minute=29), ranges) is True


def test_window_change_tolerance_and_warming():
    history: deque[tuple[float, float, float | None]] = deque()
    assert window_change(history, now=600.0, window_seconds=300) is None

    history.append((600.0 - 5 * 60, 0.001, 100.0))
    assert window_change(history, now=600.0, window_seconds=300) == 0.001

    # 超出 90s 容差 -> None
    stale: deque[tuple[float, float, float | None]] = deque([(600.0 - 5 * 60 - 120, 0.001, 100.0)])
    assert window_change(stale, now=600.0, window_seconds=300) is None

    # 历史未覆盖整个窗口 (预热) -> None
    warming: deque[tuple[float, float, float | None]] = deque([(600.0 - 60, 0.001, 100.0)])
    assert window_change(warming, now=600.0, window_seconds=300) is None


def test_volume_ratio_math():
    # 基准点: ts=60, 当日累计量 100手, 交易分钟 1 分钟 -> 日均 100手/分钟
    point = (60.0, 0.001, 100.0)
    # 窗口量 500手 / 5分钟 = 100/分钟 -> 量比 1.0
    assert volume_ratio(600.0, point, 300, now=360.0, elapsed_now=6.0) == pytest.approx(1.0)
    # 窗口放量一倍 -> 量比 2.0
    assert volume_ratio(1100.0, point, 300, now=360.0, elapsed_now=6.0) == pytest.approx(2.0)
    # 数据缺失 -> None
    assert volume_ratio(None, point, 300, now=360.0, elapsed_now=6.0) is None
    assert volume_ratio(600.0, None, 5, now=360.0, elapsed_now=6.0) is None
    # 跨日重置 (窗口量为负) -> None
    assert volume_ratio(50.0, point, 300, now=360.0, elapsed_now=6.0) is None
    # 开盘基准不足 0.5 分钟 -> None
    assert volume_ratio(600.0, point, 300, now=360.0, elapsed_now=5.2) is None


# ── 共振评估 ──────────────────────────────────────────────────

def _cn_minutes_after_open(minutes: float) -> datetime:
    return datetime(2026, 9, 10, 9, 30, tzinfo=CN_TZ) + timedelta(minutes=minutes)


class _Clock:
    """受控北京时间: 服务内 cn_now/cn_today 由测试注入。"""

    def __init__(self, monkeypatch, start: datetime):
        self._now = start
        monkeypatch.setattr(index_resonance, "cn_now", lambda: self._now)
        monkeypatch.setattr(index_resonance, "cn_today", lambda: self._now.date())

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


def _poll(service, *, ts: float, index_change_pct: float, stock_changes: dict[str, float]):
    service.update(
        _stock_df(stock_changes),
        _index_df("399006.SZ", index_change_pct),
        now=ts,
    )


def test_resonance_up_detects_top_group_and_leader(monkeypatch, tmp_path):
    service = _service(monkeypatch, tmp_path)
    service.create_monitor(_config())
    clock = _Clock(monkeypatch, _cn_minutes_after_open(0))

    # 逐分钟推进, 涨幅线性放大; 5 分钟窗口差在第 6 步可用:
    # 股票 change_pct 为小数制 (0.0024 = 0.24%/分钟):
    # 指数窗口涨幅 = 0.5% (>= 0.3% 阈值 -> up); g1 = 1.2%, g2 = 0.2%
    for step in range(1, 7):
        clock.advance(60)
        _poll(service, ts=float(step * 60), index_change_pct=0.1 * step, stock_changes={
            "600001.SH": 0.0024 * step, "600002.SH": 0.0024 * step,
            "600003.SH": 0.0004 * step, "600004.SH": 0.0004 * step,
        })

    state = service.get_state()["monitors"][0]
    assert state["config"]["id"] == "res_test"
    st = state["state"]
    assert st["status"] == "up"
    assert st["in_window"] is True
    assert st["index"]["window_change_pct"] == pytest.approx(0.5, abs=1e-6)
    assert st["index"]["change_pct"] == pytest.approx(0.6, abs=1e-6)
    assert st["index"]["price"] == pytest.approx(2000.0)
    assert st["resonant"] is True
    assert st["resonant_since"] == pytest.approx(360.0)

    # g1 窗口涨幅 1.2% > g2 0.2% -> g1 最强
    assert st["top_group_id"] == "g1"
    assert st["groups"][0]["name"] == "算力"
    assert st["groups"][0]["rank"] == 1
    assert st["groups"][0]["valid_count"] == 2
    assert st["groups"][0]["up_count"] == 2
    assert st["groups"][0]["up_ratio"] == pytest.approx(1.0)
    assert st["groups"][0]["avg_window_pct"] == pytest.approx(1.2, abs=1e-6)
    # 龙头: 窗口涨幅并列时按当前涨幅, 两者相同 -> max 取成员序先者
    assert st["leader"]["symbol"] == "600001.SH"
    assert st["leader"]["name"] == "龙头一"
    assert st["leader"]["window_change_pct"] == pytest.approx(1.2, abs=1e-6)

    # 继续推进: 共振持续, resonant_since 不重置
    clock.advance(60)
    _poll(service, ts=420.0, index_change_pct=0.7, stock_changes={
        "600001.SH": 1.5, "600002.SH": 1.5,
        "600003.SH": 0.3, "600004.SH": 0.3,
    })
    st2 = service.get_state()["monitors"][0]["state"]
    assert st2["resonant"] is True
    assert st2["resonant_since"] == pytest.approx(360.0)


def test_resonance_states_warming_down_flat_and_no_data(monkeypatch, tmp_path):
    service = _service(monkeypatch, tmp_path)
    service.create_monitor(_config())
    clock = _Clock(monkeypatch, _cn_minutes_after_open(0))

    # 仅 1 分钟历史: 预热
    clock.advance(60)
    _poll(service, ts=60.0, index_change_pct=0.5, stock_changes={
        "600001.SH": 1.0, "600002.SH": 1.0, "600003.SH": 0.0, "600004.SH": 0.0,
    })
    st = service.get_state()["monitors"][0]["state"]
    assert st["status"] == "warming"
    assert st["resonant"] is False
    assert st["groups"] == []  # 成员窗口涨幅全部未知 -> 分组不产出

    # 指数持续下行: 窗口涨幅 -0.5% -> down, 即使板块上行也不共振
    for step in range(2, 7):
        clock.advance(60)
        _poll(service, ts=float(step * 60), index_change_pct=-0.1 * step, stock_changes={
            "600001.SH": 0.0024 * step, "600002.SH": 0.0024 * step,
            "600003.SH": 0.0, "600004.SH": 0.0,
        })
    st = service.get_state()["monitors"][0]["state"]
    assert st["status"] == "down"
    assert st["resonant"] is False
    assert st["leader"] is None

    # 震荡: 窗口涨幅 0.25% 低于阈值 0.3%
    service2 = _service(monkeypatch, tmp_path)
    service2.create_monitor(_config(id="res_flat", name="震荡"))
    clock2 = _Clock(monkeypatch, _cn_minutes_after_open(0))
    for step in range(1, 7):
        clock2.advance(60)
        service2.update(
            _stock_df({"600001.SH": 0.01 * step, "600002.SH": 0.01 * step}),
            _index_df("399006.SZ", 0.05 * step),
            now=float(step * 60),
        )
    st = service2.get_state()["monitors"][0]["state"]
    assert st["status"] == "flat"
    assert st["index"]["window_change_pct"] == pytest.approx(0.25, abs=1e-6)

    # 指数快照缺失: no_data
    service3 = _service(monkeypatch, tmp_path)
    service3.create_monitor(_config(id="res_nodata", name="无数据"))
    service3.update(_stock_df({"600001.SH": 0.1}), pl.DataFrame(), now=1.0)
    st = service3.get_state()["monitors"][0]["state"]
    assert st["status"] == "no_data"
    assert st["groups"] == []


def test_resonance_group_gates_and_off_window(monkeypatch, tmp_path):
    service = _service(monkeypatch, tmp_path)
    # up_ratio 门槛 0.6: g2 一涨一跌 -> 占比 0.5 被拒, 即便平均窗口涨幅为正
    service.create_monitor(_config(id="res_gate", name="门槛"))
    service.create_monitor(_config(
        id="res_win", name="时段外",
        time_ranges=[{"start": "09:30", "end": "09:31"}],
    ))
    clock = _Clock(monkeypatch, _cn_minutes_after_open(0))
    for step in range(1, 7):
        clock.advance(60)
        _poll(service, ts=float(step * 60), index_change_pct=0.1 * step, stock_changes={
            "600001.SH": 0.0024 * step, "600002.SH": 0.0024 * step,
            "600003.SH": 0.002 * step, "600004.SH": -0.001 * step,
        })

    states = {m["config"]["id"]: m["state"] for m in service.get_state()["monitors"]}
    gate = states["res_gate"]
    assert gate["status"] == "up"
    assert gate["top_group_id"] == "g1"
    g2_row = next(g for g in gate["groups"] if g["group_id"] == "g2")
    assert g2_row["qualifying"] is False
    assert g2_row["up_ratio"] == pytest.approx(0.5)
    assert g2_row["avg_window_pct"] == pytest.approx(0.25, abs=1e-6)  # 均值为正仍被占比门槛拒绝
    assert gate["leader"]["symbol"] == "600001.SH"

    # 时段外: off_window 且不产出分组
    assert states["res_win"]["status"] == "off_window"
    assert states["res_win"]["groups"] == []


def test_resonance_min_members_and_disabled(monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    _stub_groups(monkeypatch, [
        {"id": "g1", "name": "小分组"}, {"id": "g2", "name": "正常"},
    ], members=[
        {"group_id": "g1", "symbol": "600001.SH"},  # 仅 1 个有效成员 (低于 min 2)
        {"group_id": "g2", "symbol": "600002.SH"},
        {"group_id": "g2", "symbol": "600003.SH"},
        {"group_id": "g2", "symbol": "600004.SH"},  # 停牌: 快照缺失, 不计入
    ])
    service = IndexResonanceService(_repo())
    service.create_monitor(_config(id="res_small", name="成员下限"))
    clock = _Clock(monkeypatch, _cn_minutes_after_open(0))
    for step in range(1, 7):
        clock.advance(60)
        service.update(
            _stock_df({
                "600001.SH": 0.004 * step,
                "600002.SH": 0.002 * step, "600003.SH": 0.002 * step,
            }),
            _index_df("399006.SZ", 0.1 * step),
            now=float(step * 60),
        )

    st = service.get_state()["monitors"][0]["state"]
    assert st["status"] == "up"
    g1 = next(g for g in st["groups"] if g["group_id"] == "g1")
    g2 = next(g for g in st["groups"] if g["group_id"] == "g2")
    assert g1["qualifying"] is False  # 有效成员 1 < min_group_members 2
    assert g1["valid_count"] == 1
    # g2: 600004.SH 停牌不进入有效成员, 剩余 2 只全部上涨
    assert g2["valid_count"] == 2
    assert g2["member_count"] == 3
    assert g2["qualifying"] is True
    assert st["top_group_id"] == "g2"
    assert st["resonant"] is True

    # 停用监测: 不参与实时评估, get_state 给出 disabled 静态状态
    service.update_monitor("res_small", {
        "name": "成员下限", "index_symbol": "399006.SZ", "enabled": False,
    })
    service.update(
        _stock_df({"600001.SH": 2.0}), _index_df("399006.SZ", 0.5), now=999.0,
    )
    assert service.has_enabled_monitors() is False
    st = service.get_state()["monitors"][0]["state"]
    assert st["status"] == "disabled"
    assert st["resonant"] is False


def test_history_resets_for_new_day(monkeypatch, tmp_path):
    service = _service(monkeypatch, tmp_path)
    service.create_monitor(_config())
    clock = _Clock(monkeypatch, _cn_minutes_after_open(0))
    clock.advance(60)
    _poll(service, ts=60.0, index_change_pct=0.5, stock_changes={"600001.SH": 1.0})
    assert len(service._index_hist["res_test"]) == 1

    # 跨日: 旧历史清空, 只积累新交易日数据
    clock._now = datetime(2026, 9, 11, 9, 31, tzinfo=CN_TZ)
    _poll(service, ts=86460.0, index_change_pct=0.5, stock_changes={"600001.SH": 1.0})
    assert len(service._index_hist["res_test"]) == 1


def test_three_dimension_gates(monkeypatch, tmp_path):
    """涨幅/量能门禁: 指数、板块、龙头三级各自阻断共振; 停用维度恢复纯动量行为。"""
    _isolated_store(monkeypatch, tmp_path)
    _stub_groups(monkeypatch,
        [{"id": "g1", "name": "算力"}, {"id": "g2", "name": "医药"}],
        [
            {"group_id": "g1", "symbol": "600001.SH"},
            {"group_id": "g1", "symbol": "600002.SH"},
            {"group_id": "g2", "symbol": "600003.SH"},
            {"group_id": "g2", "symbol": "600004.SH"},
        ])
    service = IndexResonanceService(_repo())
    service.create_monitor(_config(id="res_base", name="默认门禁"))
    service.create_monitor(_config(id="res_idx_chg", name="指数涨幅0.8", index_change_pct_gate=0.8))
    service.create_monitor(_config(id="res_idx_novol", name="指数量比停用", index_volume_ratio_gate=0))
    service.create_monitor(_config(id="res_leader_vol", name="龙头量比2.5", leader_volume_ratio_gate=2.5))
    service.create_monitor(_config(id="res_grp_vol", name="组量比2.0", group_volume_ratio_gate=2.0))
    clock = _Clock(monkeypatch, _cn_minutes_after_open(0))

    # 指数: 第 1 分钟 100手, 之后 200手/分钟 -> 第 6 步窗口量比 2.0 (>= 1.5)
    def idx_vol(n: int) -> float:
        return 100.0 if n == 1 else 100.0 + 200.0 * (n - 1)

    # 600001: 恒定 100手/分钟 -> 窗口量比 1.0; 600002: 放量到 200 -> 量比 2.0
    def v1(n: int) -> float:
        return 100.0 * n

    def v2(n: int) -> float:
        return 100.0 if n == 1 else 100.0 + 200.0 * (n - 1)

    for step in range(1, 7):
        clock.advance(60)
        service.update(
            _stock_df_v(
                {
                    "600001.SH": 0.0024 * step, "600002.SH": 0.002 * step,
                    "600003.SH": 0.0, "600004.SH": 0.0,
                },
                {
                    "600001.SH": v1(step), "600002.SH": v2(step),
                    "600003.SH": 100.0 * step, "600004.SH": 100.0 * step,
                },
            ),
            _index_df("399006.SZ", 0.1 * step, volume=idx_vol(step)),
            now=float(step * 60),
        )

    states = {m["config"]["id"]: m["state"] for m in service.get_state()["monitors"]}

    base = states["res_base"]
    assert base["status"] == "up"
    assert base["index"]["gates"] == {"momentum": True, "change": True, "volume": True}
    assert base["index"]["volume_ratio"] == pytest.approx(2.0)
    # g1 量比均值 1.5 >= 1.3, 涨幅均值 1.32 > 0 -> 达标; 龙头: 600001 量比 1.0 被龙头
    # 量比门禁 1.5 挡掉, 600002 (窗口 1.0%, 量比 2.0) 递补
    assert base["top_group_id"] == "g1"
    assert base["groups"][0]["qualifying"] is True
    assert base["groups"][0]["volume_ratio"] == pytest.approx(1.5)
    assert base["resonant"] is True
    assert base["leader"]["symbol"] == "600002.SH"
    assert base["leader"]["volume_ratio"] == pytest.approx(2.0)
    assert base["leader"]["window_change_pct"] == pytest.approx(1.0)

    # 指数涨幅门禁 0.8: 当前涨幅 0.6 未过 -> 即使动量达标也不判 up
    chg = states["res_idx_chg"]
    assert chg["status"] == "flat"
    assert chg["index"]["gates"]["change"] is False
    assert chg["index"]["gates"]["momentum"] is True
    assert chg["resonant"] is False

    # 指数量比停用: volume 门禁为 None (不否决), 纯动量+涨幅判定
    novol = states["res_idx_novol"]
    assert novol["status"] == "up"
    assert novol["index"]["gates"]["volume"] is None

    # 龙头量比门禁 2.5: 组内无人达标 -> 无龙头 -> 不共振 (状态与板块排行不受影响)
    lv = states["res_leader_vol"]
    assert lv["status"] == "up"
    assert lv["top_group_id"] == "g1"
    assert lv["leader"] is None
    assert lv["resonant"] is False

    # 组量比门禁 2.0: g1 量比 1.5 未过 -> 不达标, 无共振板块
    gv = states["res_grp_vol"]
    assert gv["status"] == "up"
    assert gv["top_group_id"] is None
    g1_row = next(g for g in gv["groups"] if g["group_id"] == "g1")
    assert g1_row["qualifying"] is False
    assert g1_row["gates"]["volume"] is False
    assert g1_row["gates"]["momentum"] is True


def test_resonance_notification_edge_and_cooldown(monkeypatch, tmp_path):
    """共振上升沿 -> 一条通知事件; 持续共振不重发; 冷却内翻飞被抑制, 冷却后放行。"""
    service = _service(monkeypatch, tmp_path)
    service.create_monitor(_config(
        id="res_notify", name="推送测试",
        webhook_channels=["feishu", "bogus", "email"],
        notify_cooldown_seconds=600,
    ))
    clock = _Clock(monkeypatch, _cn_minutes_after_open(0))

    def _rising(step: int):
        _poll(service, ts=float(step * 60), index_change_pct=0.1 * step, stock_changes={
            "600001.SH": 0.0024 * step, "600002.SH": 0.0024 * step,
            "600003.SH": 0.0, "600004.SH": 0.0,
        })

    for step in range(1, 7):
        clock.advance(60)
        _rising(step)

    events = service.consume_events()
    assert len(events) == 1
    ev = events[0]
    assert ev["source"] == "resonance"
    assert ev["type"] == "resonance_up"
    assert ev["rule_id"] == "res_notify"
    assert ev["symbol"] == "399006.SZ"
    assert ev["webhook_channels"] == ["feishu", "email"]  # 非法渠道被过滤
    assert ev["severity"] == "info"
    assert "算力" in ev["message"] and "龙头" in ev["message"]
    assert ev["resonance"]["group"]["group_id"] == "g1"
    assert ev["resonance"]["leader"]["symbol"] == "600001.SH"

    # 持续共振 (同一时刻重复轮询): 不再产生事件; consume 已清空
    _poll(service, ts=360.0, index_change_pct=0.6, stock_changes={
        "600001.SH": 0.0144, "600002.SH": 0.0144,
        "600003.SH": 0.0, "600004.SH": 0.0,
    })
    assert service.consume_events() == []

    # 冷却内反复翻飞 (奇数步跌、偶数步升): 上升沿连续出现但全部被 600s 冷却抑制
    for step in range(7, 17):
        clock.advance(60)
        _poll(service, ts=float(step * 60), index_change_pct=0.9 if step % 2 == 0 else -0.5, stock_changes={
            "600001.SH": 0.0024 * step, "600002.SH": 0.0024 * step,
            "600003.SH": 0.0, "600004.SH": 0.0,
        })
    events = service.consume_events()
    assert len(events) == 1  # ts=960 的上升沿距上次通知恰好 600s -> 放行
    assert events[0]["ts"] == 960_000


def test_resonance_with_30s_window(monkeypatch, tmp_path):
    """30 秒窗口: 亚分钟动量即可触发共振 (开盘急拉场景)。"""
    service = _service(monkeypatch, tmp_path)
    service.create_monitor(_config(
        id="res_fast", name="极速", window_seconds=30, index_threshold_pct=0.3,
    ))
    clock = _Clock(monkeypatch, _cn_minutes_after_open(0))

    # 每 10 秒一个快照; 第 4 拍 (ts=40) 窗口覆盖 30s:
    # 指数窗口涨幅 = 0.3% (0.4-0.1) 达阈值; g1 窗口涨幅 0.72% 且全部上涨
    for tick in range(1, 5):
        clock.advance(10)
        _poll(service, ts=float(tick * 10), index_change_pct=0.1 * tick, stock_changes={
            "600001.SH": 0.0024 * tick, "600002.SH": 0.0024 * tick,
            "600003.SH": 0.0, "600004.SH": 0.0,
        })

    st = service.get_state()["monitors"][0]["state"]
    assert st["status"] == "up"
    assert st["index"]["window_change_pct"] == pytest.approx(0.3, abs=1e-6)
    assert st["top_group_id"] == "g1"
    assert st["leader"]["symbol"] == "600001.SH"
    assert st["leader"]["window_change_pct"] == pytest.approx(0.72, abs=1e-6)
    # 通知事件同步产生 (默认冷却, 上升沿), 文案用"30秒"标签
    events = service.consume_events()
    assert len(events) == 1
    assert "30秒" in events[0]["message"]


def test_scoped_symbols_mixed_all_and_explicit(monkeypatch, tmp_path):
    """一个监测全部分组 + 一个监测指定分组: 成员并集不能漏掉全分组监测的成员。"""
    _isolated_store(monkeypatch, tmp_path)
    _stub_groups(monkeypatch, [
        {"id": "g1", "name": "算力"}, {"id": "g2", "name": "医药"},
    ], members=[
        {"group_id": "g1", "symbol": "600001.SH"},
        {"group_id": "g2", "symbol": "600003.SH"},
    ])
    monitors = [
        {"group_ids": []},                 # 全部分组
        {"group_ids": ["g1"]},             # 仅 g1
    ]
    scoped = index_resonance._scoped_symbols(monitors, {"members": [
        {"group_id": "g1", "symbol": "600001.SH"},
        {"group_id": "g2", "symbol": "600003.SH"},
    ]})
    assert scoped == {"600001.SH", "600003.SH"}

    scoped_explicit = index_resonance._scoped_symbols(
        [{"group_ids": ["g2"]}],
        {"members": [
            {"group_id": "g1", "symbol": "600001.SH"},
            {"group_id": "g2", "symbol": "600003.SH"},
        ]},
    )
    assert scoped_explicit == {"600003.SH"}


# ── API 契约 ──────────────────────────────────────────────────

def _request_with_service(service):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(resonance_service=service)))


def test_api_monitor_crud_and_validation(monkeypatch, tmp_path):
    service = _service(monkeypatch, tmp_path)
    request = _request_with_service(service)

    payload = resonance_api.MonitorUpsertRequest(
        name="创业板共振", index_symbol="399006.SZ",
        time_ranges=[{"start": "09:30", "end": "10:30"}],
        index_threshold_pct=0.4, group_up_ratio=0.5, min_group_members=2,
        group_ids=["g1"],
    )
    result = resonance_api.create_monitor(request, payload)
    assert len(result["monitors"]) == 1
    monitor_id = result["monitors"][0]["id"]
    assert result["monitors"][0]["time_ranges"] == [{"start": 570, "end": 630}]

    updated = resonance_api.update_monitor(
        request, monitor_id,
        resonance_api.MonitorUpsertRequest(name="改名", index_symbol="000001.SH"),
    )
    assert updated["monitors"][0]["index_symbol"] == "000001.SH"
    assert updated["monitors"][0]["time_ranges"] == []  # 未传时段 -> 清空

    state = resonance_api.resonance_state(request)
    assert state["monitors"][0]["config"]["id"] == monitor_id
    # 服务尚未在盘中运行过 -> 空闲状态
    assert state["monitors"][0]["state"]["status"] == "off_window"

    with pytest.raises(ValidationError):
        resonance_api.MonitorUpsertRequest(name="非法", index_symbol="888888.SZ")

    resonance_api.delete_monitor(request, monitor_id)
    assert resonance_api.list_monitors(request)["monitors"] == []


def test_api_missing_service_returns_503():
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    with pytest.raises(HTTPException) as excinfo:
        resonance_api.list_monitors(request)
    assert excinfo.value.status_code == 503
