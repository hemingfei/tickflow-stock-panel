"""看板快照落盘与回溯测试: 落盘刻度判断、写入/回读、最近节点语义、
同分钟去重与回溯 API 端点。"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import board_snapshots as snapshots_api
from app.services import board_snapshot_store as store


def _dt(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm)


def _state(tmp_path):
    return SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))


# ---------- 落盘刻度判断 ----------

@pytest.mark.parametrize("moment,expected", [
    # 交易日 (2026-09-02 周三): 两个时段起点/收盘/整 5 分钟刻度
    (_dt(2026, 9, 2, 9, 30), True),
    (_dt(2026, 9, 2, 9, 35), True),
    (_dt(2026, 9, 2, 11, 30), True),
    (_dt(2026, 9, 2, 13, 0), True),
    (_dt(2026, 9, 2, 15, 0), True),
    # 非刻度: 开盘前/盘中非对齐/午休/收盘后
    (_dt(2026, 9, 2, 9, 29), False),
    (_dt(2026, 9, 2, 9, 32), False),
    (_dt(2026, 9, 2, 11, 31), False),
    (_dt(2026, 9, 2, 12, 59), False),
    (_dt(2026, 9, 2, 15, 1), False),
    # 周末
    (_dt(2026, 9, 5, 10, 0), False),
    (_dt(2026, 9, 6, 13, 5), False),
])
def test_is_aligned_record_tick(moment, expected):
    assert store.is_aligned_record_tick(moment) is expected


# ---------- 解析与路径安全 ----------

def test_normalize_inputs():
    assert store.normalize_day("2026-9-1") == "2026-09-01"
    assert store.normalize_hhmm("9:5") == "09:05"
    for bad_day in ("2026-13-01", "abc", "../../etc"):
        with pytest.raises(ValueError):
            store.normalize_day(bad_day)
    for bad_hhmm in ("25:00", "x", ""):
        with pytest.raises(ValueError):
            store.normalize_hhmm(bad_hhmm)


def test_snapshot_path_is_sanitized(tmp_path):
    # 外部输入即使带分隔符也被解析归一, 不可能拼出穿越路径
    p = store.snapshot_path(tmp_path, "2026-09-01", "09:35")
    assert p == tmp_path / "board_snapshots" / "2026-09-01" / "0935.json"


# ---------- 写入与回读 ----------

def test_record_and_load_roundtrip(tmp_path):
    store.set_snapshot_builder(lambda state: {"as_of": "2026-09-02", "emotion": {"score": 66}})
    result = store.record_snapshot(_state(tmp_path), now=_dt(2026, 9, 2, 9, 35))
    assert result["date"] == "2026-09-02"
    assert result["time"] == "09:35"
    assert (tmp_path / "board_snapshots" / "2026-09-02" / "0935.json").is_file()
    # 临时文件不留残骸
    assert not list((tmp_path / "board_snapshots" / "2026-09-02").glob("*.tmp"))

    store.record_snapshot(_state(tmp_path), now=_dt(2026, 9, 2, 9, 40))
    assert store.list_dates(tmp_path) == ["2026-09-02"]
    assert store.list_times(tmp_path, "2026-09-02") == ["09:35", "09:40"]
    # 无快照日期 → 空列表; 非法日期 → ValueError
    assert store.list_times(tmp_path, "2026-09-03") == []
    with pytest.raises(ValueError):
        store.list_times(tmp_path, "bad")


def test_load_nearest_floor_and_fallbacks(tmp_path):
    store.set_snapshot_builder(lambda state: {"as_of": "2026-09-02"})
    for hh, mm in ((9, 30), (9, 35), (10, 45)):
        store.record_snapshot(_state(tmp_path), now=_dt(2026, 9, 2, hh, mm))

    # 不晚于目标的最近节点
    assert store.load_nearest(tmp_path, "2026-09-02", "09:33")[0] == "09:30"
    assert store.load_nearest(tmp_path, "2026-09-02", "09:35")[0] == "09:35"
    assert store.load_nearest(tmp_path, "2026-09-02", "23:59")[0] == "10:45"
    # 早于首节点 → 回退首节点 (拖到开盘前仍有内容)
    assert store.load_nearest(tmp_path, "2026-09-02", "08:00")[0] == "09:30"
    # 缺省时刻 → 当日最后一个节点
    assert store.load_nearest(tmp_path, "2026-09-02")[0] == "10:45"
    # 该日期无快照 → None
    assert store.load_nearest(tmp_path, "2026-09-03", "10:00") is None


def test_run_due_record_aligned_and_dedup(tmp_path):
    store.set_snapshot_builder(lambda state: {"as_of": "2026-09-02"})
    state = _state(tmp_path)
    store.reset_record_state()

    # 非刻度 → 不落盘
    assert store.run_due_record(state, now=_dt(2026, 9, 2, 9, 32)) is None
    assert store.list_dates(tmp_path) == []
    # 刻度 → 落盘; 同一分钟重复 fire 去重
    first = store.run_due_record(state, now=_dt(2026, 9, 2, 9, 35))
    assert first is not None and first["time"] == "09:35"
    assert store.run_due_record(state, now=_dt(2026, 9, 2, 9, 35)) is None
    # 下一刻度继续落盘
    assert store.run_due_record(state, now=_dt(2026, 9, 2, 9, 40)) is not None
    assert store.list_times(tmp_path, "2026-09-02") == ["09:35", "09:40"]


def test_run_due_record_builder_failure_is_swallowed(tmp_path):
    def _broken(state):
        raise RuntimeError("no data")

    store.set_snapshot_builder(_broken)
    store.reset_record_state()
    assert store.run_due_record(_state(tmp_path), now=_dt(2026, 9, 2, 9, 35)) is None
    # 构建失败不写文件, 也不影响同分钟之后的重试语义 (仅记警告)
    assert store.list_dates(tmp_path) == []


def test_record_without_builder_raises(tmp_path):
    store.set_snapshot_builder(None)
    with pytest.raises(RuntimeError):
        store.record_snapshot(_state(tmp_path), now=_dt(2026, 9, 2, 9, 35))


# ---------- 回溯 API 端点 ----------

@pytest.fixture
def client(tmp_path):
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(snapshots_api.router)
    app.include_router(snapshots_api.public_router)
    return TestClient(app), tmp_path


def test_api_dates_times_load(client):
    api_client, tmp_path = client
    store.set_snapshot_builder(lambda state: {"as_of": "2026-09-02", "overview": {}})
    store.record_snapshot(_state(tmp_path), now=_dt(2026, 9, 2, 9, 35))
    store.record_snapshot(_state(tmp_path), now=_dt(2026, 9, 2, 9, 40))

    assert api_client.get("/api/board-snapshots/dates").json() == {"dates": ["2026-09-02"]}
    r = api_client.get("/api/board-snapshots/times", params={"date": "2026-09-02"})
    assert r.status_code == 200
    assert r.json() == {"date": "2026-09-02", "times": ["09:35", "09:40"]}

    r = api_client.get("/api/board-snapshots/load", params={"date": "2026-09-02", "time": "09:38"})
    assert r.status_code == 200
    body = r.json()
    assert body["snapshot_time"] == "09:35"
    assert body["requested_time"] == "09:38"
    assert body["snapshot"]["as_of"] == "2026-09-02"

    # 缺省 date → 最新日期; 缺省 time → 当日最后节点
    r = api_client.get("/api/board-snapshots/load")
    assert r.status_code == 200
    assert r.json()["snapshot_time"] == "09:40"


def test_api_errors(client):
    api_client, tmp_path = client
    # 非法参数 → 400
    assert api_client.get("/api/board-snapshots/times", params={"date": "bad"}).status_code == 400
    assert api_client.get(
        "/api/board-snapshots/load", params={"date": "2026-09-02", "time": "9点"}).status_code == 400
    # 无任何快照 → 404
    assert api_client.get("/api/board-snapshots/load").status_code == 404
    # 有快照的日期之外 → 404
    store.set_snapshot_builder(lambda state: {"as_of": "2026-09-02"})
    store.record_snapshot(_state(tmp_path), now=_dt(2026, 9, 2, 9, 35))
    assert api_client.get("/api/board-snapshots/load", params={"date": "2026-09-03"}).status_code == 404
    assert api_client.get("/api/board-snapshots/times", params={"date": "2026-09-03"}).status_code == 200


# ---------- 公开回放 API (免登录独立页) ----------

def test_api_public_load_strips_alerts(client):
    """公开端点剥离监控中心告警 (含策略名/自选标的), 行情数据保持完整。"""
    api_client, tmp_path = client
    store.set_snapshot_builder(lambda state: {
        "as_of": "2026-09-02",
        "overview": {"emotion": {"score": 31}},
        "alerts": {"alerts": [{"symbol": "600000.SH", "message": "触发策略「私密策略」"}], "total": 1},
    })
    store.record_snapshot(_state(tmp_path), now=_dt(2026, 9, 2, 9, 35))

    r = api_client.get("/api/public/replay/load", params={"date": "2026-09-02", "time": "10:00"})
    assert r.status_code == 200
    body = r.json()
    assert body["snapshot_time"] == "09:35"
    assert body["snapshot"]["alerts"] == {"alerts": [], "total": 0}
    assert body["snapshot"]["overview"]["emotion"]["score"] == 31

    assert api_client.get("/api/public/replay/dates").json() == {"dates": ["2026-09-02"]}
    assert api_client.get("/api/public/replay/times", params={"date": "2026-09-02"}).json()["times"] == ["09:35"]
    # 错误路径与站内口径一致
    assert api_client.get("/api/public/replay/load", params={"date": "bad"}).status_code == 400
    assert api_client.get("/api/public/replay/load", params={"date": "2026-09-03"}).status_code == 404


def test_api_public_stays_read_over_internal_data(client):
    """公开剥离只影响公开端点响应, 不改动落盘的快照文件本身。"""
    api_client, tmp_path = client
    store.set_snapshot_builder(lambda state: {
        "as_of": "2026-09-02",
        "alerts": {"alerts": [{"symbol": "600000.SH"}], "total": 1},
    })
    store.record_snapshot(_state(tmp_path), now=_dt(2026, 9, 2, 9, 35))
    api_client.get("/api/public/replay/load")
    # 站内端点仍返回完整告警
    internal = api_client.get("/api/board-snapshots/load").json()
    assert internal["snapshot"]["alerts"]["total"] == 1


def test_auth_whitelist_bypasses_login_for_public_replay(tmp_path, monkeypatch):
    """已设密码时: 公开回放端点免会话可访问, 站内端点仍 401。"""
    from app.main import app
    from app.services import auth as auth_service

    monkeypatch.setattr(auth_service, "is_configured", lambda: True)
    monkeypatch.setattr(
        app.state, "repo", SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)), raising=False)
    client = TestClient(app)
    assert client.get("/api/board-snapshots/dates").status_code == 401
    assert client.get("/api/public/replay/dates").status_code == 200
