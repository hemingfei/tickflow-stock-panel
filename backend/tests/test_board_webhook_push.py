"""看板快照定时推送测试: 时间判断纯函数、偏好读写、通用 Webhook 发送、
快照结构、settings API 端点与推送状态记录。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
from datetime import datetime
from datetime import time as dt_time
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api import overview as overview_api
from app.api import settings as settings_api
from app.services import board_webhook_push as push_svc
from app.services import preferences, webhook_adapter

# ---------- 公共夹具 ----------

@pytest.fixture
def prefs_dir(tmp_path, monkeypatch):
    """把 preferences.json 指向临时目录, 避免读写真实用户数据。"""
    p = tmp_path / "user_data" / "preferences.json"
    p.parent.mkdir(parents=True, exist_ok=True)  # 真实 _path() 会建父目录, 夹具保持一致
    monkeypatch.setattr(preferences, "_path", lambda: p)
    return tmp_path


@pytest.fixture(autouse=True)
def _reset_push_state():
    """推送状态是模块级内存态, 测试间必须复位。"""
    push_svc.set_snapshot_builder(None)
    push_svc._last_attempt_at = None
    push_svc._last_success_at = None
    push_svc._last_ok = None
    push_svc._last_detail = ""
    push_svc._last_as_of = None
    push_svc._last_format = None
    push_svc._recent.clear()
    push_svc._last_push_minute = None
    yield


def _cfg(**overrides) -> dict:
    cfg = {
        "enabled": True,
        "webhook_url": "https://example.com/hook",
        "windows": [{"start_time": "09:30", "end_time": "15:00", "interval_minutes": 5}],
    }
    cfg.update(overrides)
    return cfg


# ---------- 时间判断纯函数 ----------

def test_parse_hhmm():
    assert push_svc.parse_hhmm("09:30") == dt_time(9, 30)
    assert push_svc.parse_hhmm("15:05") == dt_time(15, 5)
    # strptime 对 %H/%M 接受单位数 ("9:5"), 保存时会归一化为 "09:05", 属宽松行为
    assert push_svc.parse_hhmm("9:5") == dt_time(9, 5)
    for bad in ("9点30", "25:00", "", "09:30:00", "9-30"):
        with pytest.raises(ValueError):
            push_svc.parse_hhmm(bad)


def test_in_push_window_boundaries_inclusive():
    start, end = dt_time(9, 30), dt_time(15, 0)
    assert push_svc.in_push_window(dt_time(9, 30), start, end)
    assert push_svc.in_push_window(dt_time(15, 0), start, end)
    assert push_svc.in_push_window(dt_time(11, 47), start, end)
    assert not push_svc.in_push_window(dt_time(9, 29), start, end)
    assert not push_svc.in_push_window(dt_time(15, 1), start, end)


def test_in_push_window_fail_closed_on_inverted_or_equal():
    assert not push_svc.in_push_window(dt_time(10, 0), dt_time(15, 0), dt_time(9, 30))
    assert not push_svc.in_push_window(dt_time(10, 0), dt_time(10, 0), dt_time(10, 0))


def test_is_aligned_tick():
    start = dt_time(9, 30)
    assert not push_svc.is_aligned_tick(datetime(2026, 9, 1, 9, 30), start, 5)  # 开始只是计时起点, 不触发
    assert push_svc.is_aligned_tick(datetime(2026, 9, 1, 9, 35), start, 5)  # 首个触发点 = 开始+间隔
    assert push_svc.is_aligned_tick(datetime(2026, 9, 1, 14, 55), start, 5)
    assert not push_svc.is_aligned_tick(datetime(2026, 9, 1, 9, 37), start, 5)
    assert not push_svc.is_aligned_tick(datetime(2026, 9, 1, 9, 25), start, 5)  # 开始前
    assert not push_svc.is_aligned_tick(datetime(2026, 9, 1, 9, 30), start, 0)  # 非法间隔


def test_should_push_now_truth_table():
    # 2026-09-01 为周二
    assert not push_svc.should_push_now(datetime(2026, 9, 1, 9, 30), _cfg())  # 开始时刻不触发
    assert push_svc.should_push_now(datetime(2026, 9, 1, 9, 35), _cfg())
    assert not push_svc.should_push_now(datetime(2026, 9, 1, 9, 37), _cfg())  # 未对齐
    assert not push_svc.should_push_now(datetime(2026, 9, 1, 9, 35), _cfg(enabled=False))
    assert not push_svc.should_push_now(datetime(2026, 9, 5, 9, 35), _cfg())  # 周六
    assert not push_svc.should_push_now(datetime(2026, 9, 6, 9, 35), _cfg())  # 周日
    assert not push_svc.should_push_now(datetime(2026, 9, 1, 9, 0), _cfg())  # 窗口外
    assert not push_svc.should_push_now(datetime(2026, 9, 1, 9, 35), _cfg(windows=[]))  # 无窗口
    assert not push_svc.should_push_now(
        datetime(2026, 9, 1, 9, 35), _cfg(windows=[{"start_time": "bad", "end_time": "15:00",
                                                    "interval_minutes": 5}]))  # 配置非法 fail-closed


def test_should_push_now_multi_windows_each_own_interval():
    # 典型 A 股两段: 早盘 + 午后, 午休与收盘后不推; 每窗独立间隔
    cfg = _cfg(windows=[{"start_time": "09:30", "end_time": "11:30", "interval_minutes": 5},
                        {"start_time": "13:00", "end_time": "15:00", "interval_minutes": 10}])
    assert push_svc.should_push_now(datetime(2026, 9, 1, 9, 35), cfg)
    assert not push_svc.should_push_now(datetime(2026, 9, 1, 13, 0), cfg)  # 窗口二起点, 不触发
    assert not push_svc.should_push_now(datetime(2026, 9, 1, 13, 5), cfg)  # 13:00+5 ≠ 10 的对齐点
    assert push_svc.should_push_now(datetime(2026, 9, 1, 13, 10), cfg)     # 13:00+10
    assert not push_svc.should_push_now(datetime(2026, 9, 1, 12, 0), cfg)  # 午休
    assert not push_svc.should_push_now(datetime(2026, 9, 1, 11, 45), cfg)  # 两窗之间
    # 每个窗口按自己的起点对齐, 不沿用另一窗的对齐网格
    cfg2 = _cfg(windows=[{"start_time": "09:15", "end_time": "11:30", "interval_minutes": 5},
                         {"start_time": "13:03", "end_time": "15:00", "interval_minutes": 5}])
    assert push_svc.should_push_now(datetime(2026, 9, 1, 13, 8), cfg2)      # 13:03+5
    assert not push_svc.should_push_now(datetime(2026, 9, 1, 13, 5), cfg2)  # 在窗二内但对齐窗一起点 → 不推
    assert push_svc.should_push_now(datetime(2026, 9, 1, 11, 15), cfg2)     # 窗口一 (09:15+120)


def test_parse_windows_skips_incomplete():
    assert push_svc.parse_windows(_cfg()) == [(dt_time(9, 30), dt_time(15, 0), 5)]
    assert push_svc.parse_windows(_cfg(windows=[])) == []
    # 半窗/非法时间/非法间隔静默跳过 (fail-closed)
    assert push_svc.parse_windows(
        _cfg(windows=[{"start_time": "09:30", "end_time": "", "interval_minutes": 5}])) == []
    assert push_svc.parse_windows(
        _cfg(windows=[{"start_time": "bad", "end_time": "15:00", "interval_minutes": 5}])) == []
    assert push_svc.parse_windows(
        _cfg(windows=[{"start_time": "09:30", "end_time": "15:00", "interval_minutes": 0}])) == []
    assert push_svc.parse_windows(_cfg(windows=["junk", 42])) == []
    two = push_svc.parse_windows(_cfg(windows=[
        {"start_time": "09:30", "end_time": "11:30", "interval_minutes": 5},
        {"start_time": "13:00", "end_time": "15:00", "interval_minutes": 10}]))
    assert two == [(dt_time(9, 30), dt_time(11, 30), 5), (dt_time(13, 0), dt_time(15, 0), 10)]


def test_parse_windows_legacy_flat_fields_fallback():
    """缺 windows 字段时回退读旧版平铺字段 (全局共用一个间隔)。"""
    legacy = {
        "enabled": True, "webhook_url": "https://example.com/hook",
        "start_time": "09:30", "end_time": "11:30",
        "start_time_2": "13:00", "end_time_2": "15:00",
        "interval_minutes": 7,
    }
    assert push_svc.parse_windows(legacy) == [
        (dt_time(9, 30), dt_time(11, 30), 7), (dt_time(13, 0), dt_time(15, 0), 7)]
    assert push_svc.parse_windows({}) == []


# ---------- preferences 读写 ----------

def test_webhook_push_schedule_defaults_and_roundtrip(prefs_dir):
    assert preferences.get_webhook_push_schedule() == {
        "enabled": False, "format": "generic", "webhook_url": "", "feishu_secret": "",
        "windows": [{"start_time": "09:30", "end_time": "11:30", "interval_minutes": 5}],
    }
    saved = preferences.set_webhook_push_schedule(
        True, "https://open.feishu.cn/open-apis/bot/v2/hook/abc",
        [{"start_time": "09:15", "end_time": "11:35", "interval_minutes": 7},
         {"start_time": "13:05", "end_time": "15:05", "interval_minutes": 15}],
        "feishu", "mysecret")
    assert saved == {
        "enabled": True, "format": "feishu",
        "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/abc",
        "feishu_secret": "mysecret",
        "windows": [{"start_time": "09:15", "end_time": "11:35", "interval_minutes": 7},
                    {"start_time": "13:05", "end_time": "15:05", "interval_minutes": 15}],
    }
    assert preferences.get_webhook_push_schedule() == saved


def test_webhook_push_schedule_legacy_flat_fields_migrated(prefs_dir):
    """旧版平铺配置 (两窗 + 全局间隔) 读取时迁移为 windows 数组。"""
    preferences.save({"webhook_push_schedule": {
        "enabled": True, "format": "feishu", "webhook_url": "https://example.com/hook",
        "feishu_secret": "sec",
        "start_time": "09:30", "end_time": "11:30",
        "start_time_2": "13:00", "end_time_2": "15:00",
        "interval_minutes": 8,
    }})
    cfg = preferences.get_webhook_push_schedule()
    assert cfg["enabled"] is True and cfg["format"] == "feishu"
    assert cfg["windows"] == [
        {"start_time": "09:30", "end_time": "11:30", "interval_minutes": 8},
        {"start_time": "13:00", "end_time": "15:00", "interval_minutes": 8},
    ]
    # 旧配置里第二窗为空 → 只迁移出一个窗口
    preferences.save({"webhook_push_schedule": {
        "start_time": "09:30", "end_time": "11:30",
        "start_time_2": "", "end_time_2": "", "interval_minutes": 3}})
    assert preferences.get_webhook_push_schedule()["windows"] == [
        {"start_time": "09:30", "end_time": "11:30", "interval_minutes": 3}]


def test_webhook_push_schedule_get_normalizes_bad_windows(prefs_dir):
    """读取时容错: 非法窗口剔除、间隔裁剪、超上限丢弃、全非法回退默认单窗。"""
    preferences.save({"webhook_push_schedule": {"windows": [
        {"start_time": "09:05", "end_time": "11:30", "interval_minutes": 500},   # 间隔裁剪
        {"start_time": "13:00", "end_time": "", "interval_minutes": 5},          # 半窗剔除
        {"start_time": "bad", "end_time": "15:00", "interval_minutes": 5},       # 非法剔除
        {"start_time": "14:00", "end_time": "15:00", "interval_minutes": 5},
    ] + [{"start_time": f"1{i}:00", "end_time": f"1{i}:30", "interval_minutes": 5}
         for i in range(6)]}})  # 超出上限的窗口被丢弃
    cfg = preferences.get_webhook_push_schedule()
    assert cfg["windows"][0]["interval_minutes"] == 120
    assert len(cfg["windows"]) == preferences.WEBHOOK_PUSH_MAX_WINDOWS
    # windows 全非法 → 回退默认单窗
    preferences.save({"webhook_push_schedule": {"windows": [{"start_time": "x"}]}})
    assert preferences.get_webhook_push_schedule()["windows"] == [
        {"start_time": "09:30", "end_time": "11:30", "interval_minutes": 5}]


def test_webhook_push_schedule_interval_clamped(prefs_dir):
    lo = preferences.set_webhook_push_schedule(
        False, "", [{"start_time": "09:15", "end_time": "11:35", "interval_minutes": 0}])
    hi = preferences.set_webhook_push_schedule(
        False, "", [{"start_time": "09:15", "end_time": "11:35", "interval_minutes": 500}])
    assert lo["windows"][0]["interval_minutes"] == 1
    assert hi["windows"][0]["interval_minutes"] == 120


def test_webhook_push_schedule_validation(prefs_dir):
    win = {"start_time": "09:30", "end_time": "11:30", "interval_minutes": 5}
    with pytest.raises(ValueError):  # 启用但 URL 非法
        preferences.set_webhook_push_schedule(True, "ftp://x", [win])
    with pytest.raises(ValueError):  # 时间格式
        preferences.set_webhook_push_schedule(
            False, "", [{"start_time": "930", "end_time": "11:30", "interval_minutes": 5}])
    with pytest.raises(ValueError):  # 窗口倒置
        preferences.set_webhook_push_schedule(
            False, "", [{"start_time": "15:00", "end_time": "09:30", "interval_minutes": 5}])
    with pytest.raises(ValueError):  # 起止相等
        preferences.set_webhook_push_schedule(
            False, "", [{"start_time": "09:30", "end_time": "09:30", "interval_minutes": 5}])
    with pytest.raises(ValueError):  # 只填开始
        preferences.set_webhook_push_schedule(
            False, "", [{"start_time": "13:00", "end_time": "", "interval_minutes": 5}])
    with pytest.raises(ValueError):  # 时间窗列表为空
        preferences.set_webhook_push_schedule(False, "", [])
    with pytest.raises(ValueError):  # 超出窗口数上限
        preferences.set_webhook_push_schedule(
            False, "", [win] * (preferences.WEBHOOK_PUSH_MAX_WINDOWS + 1))
    with pytest.raises(ValueError):  # 飞书格式但地址非飞书
        preferences.set_webhook_push_schedule(
            True, "https://example.com/hook", [win], format="feishu")
    with pytest.raises(ValueError):  # 非法格式
        preferences.set_webhook_push_schedule(
            False, "https://example.com/hook", [win], format="dingtalk")
    # kol 格式: 任意 http/s 地址可用 (系统 KOL Webhook)
    kol_cfg = preferences.set_webhook_push_schedule(
        True, "https://vpush.example.com/api/kol-webhook/tok", [win], format="kol")
    assert kol_cfg["format"] == "kol"
    # 多窗上限内保存成功, 各窗独立间隔
    multi = preferences.set_webhook_push_schedule(
        False, "", [win, {"start_time": "13:00", "end_time": "15:00", "interval_minutes": 30}])
    assert multi["windows"] == [
        win, {"start_time": "13:00", "end_time": "15:00", "interval_minutes": 30}]


# ---------- 通用 Webhook 发送 ----------

class _Resp:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


def test_send_generic_webhook_success():
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append(url)
        return _Resp(200)

    with mock.patch("httpx.post", fake_post):
        ok, detail = webhook_adapter.send_generic_webhook("https://example.com/hook", {"a": 1})
    assert ok
    assert "200" in detail
    assert len(calls) == 1


def test_send_generic_webhook_client_error_no_retry():
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append(url)
        return _Resp(404, "nope")

    with mock.patch("httpx.post", fake_post):
        ok, detail = webhook_adapter.send_generic_webhook("https://example.com/hook", {})
    assert not ok
    assert "404" in detail
    assert len(calls) == 1  # 4xx 不重试


def test_send_generic_webhook_server_error_retries_then_fails(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "httpx.post",
        lambda url, json=None, timeout=None: calls.append(url) or _Resp(500, "boom"),
    )
    monkeypatch.setattr(webhook_adapter.time, "sleep", lambda s: None)
    ok, detail = webhook_adapter.send_generic_webhook("https://example.com/hook", {})
    assert not ok
    assert "500" in detail
    assert len(calls) == 3  # 网络类失败重试至上限


def test_send_generic_webhook_retries_on_network_error(monkeypatch):
    calls = []

    def flaky(url, json=None, timeout=None):
        calls.append(url)
        if len(calls) < 2:
            raise ConnectionError("reset")
        return _Resp(200)

    monkeypatch.setattr("httpx.post", flaky)
    monkeypatch.setattr(webhook_adapter.time, "sleep", lambda s: None)
    ok, _ = webhook_adapter.send_generic_webhook("https://example.com/hook", {})
    assert ok
    assert len(calls) == 2


def test_send_generic_webhook_rejects_non_http():
    ok, detail = webhook_adapter.send_generic_webhook("notaurl", {})
    assert not ok
    assert "http" in detail.lower()


# ---------- push_now / run_due_push / 状态 ----------

def test_push_now_invalid_url_records_failure(prefs_dir):
    result = push_svc.push_now(object(), webhook_url="bad")
    assert result["ok"] is False
    status = push_svc.get_push_status()
    assert status["last_ok"] is False
    assert status["recent"][0]["ok"] is False


def test_push_now_snapshot_failure_recorded(prefs_dir):
    def boom(state):
        raise RuntimeError("no data")

    push_svc.set_snapshot_builder(boom)
    result = push_svc.push_now(object(), webhook_url="https://example.com/hook")
    assert result["ok"] is False
    assert "快照构建失败" in result["detail"]
    assert push_svc.get_push_status()["last_ok"] is False


def test_run_due_push_skips_outside_window(prefs_dir):
    preferences.set_webhook_push_schedule(
        True, "https://example.com/hook",
        [{"start_time": "09:30", "end_time": "15:00", "interval_minutes": 5}])
    builder_calls = []
    push_svc.set_snapshot_builder(lambda state: builder_calls.append(state) or {})
    # 20:35 在窗口外; 9:37 未对齐; 9:30 为窗口起点 (只计时, 不触发) → 均不应构建快照
    assert push_svc.run_due_push(object(), now=datetime(2026, 9, 1, 20, 35)) is None
    assert push_svc.run_due_push(object(), now=datetime(2026, 9, 1, 9, 37)) is None
    assert push_svc.run_due_push(object(), now=datetime(2026, 9, 1, 9, 30)) is None
    assert builder_calls == []


def test_run_due_push_sends_snapshot_on_aligned_tick(prefs_dir, monkeypatch):
    preferences.set_webhook_push_schedule(
        True, "https://example.com/hook",
        [{"start_time": "09:30", "end_time": "15:00", "interval_minutes": 5}])
    snapshot = {"schema_version": 1, "as_of": "2026-09-01", "overview": {}}
    push_svc.set_snapshot_builder(lambda state: snapshot)
    sent = {}
    monkeypatch.setattr(
        webhook_adapter, "send_generic_webhook",
        lambda url, payload: sent.update(url=url, payload=payload) or (True, "HTTP 200"),
    )
    result = push_svc.run_due_push(object(), now=datetime(2026, 9, 1, 9, 35, 20))
    assert result is not None and result["ok"] is True
    assert sent["payload"] is snapshot  # 快照原样作为 JSON body
    status = push_svc.get_push_status()
    assert status["last_ok"] is True
    assert status["last_as_of"] == "2026-09-01"
    assert status["last_success_at"] is not None


def test_run_due_push_dedupes_same_minute(prefs_dir, monkeypatch):
    preferences.set_webhook_push_schedule(
        True, "https://example.com/hook",
        [{"start_time": "09:30", "end_time": "15:00", "interval_minutes": 5}])
    push_svc.set_snapshot_builder(lambda state: {"as_of": "d"})
    monkeypatch.setattr(
        webhook_adapter, "send_generic_webhook", lambda url, payload: (True, "HTTP 200"))
    first = push_svc.run_due_push(object(), now=datetime(2026, 9, 1, 9, 35, 10))
    second = push_svc.run_due_push(object(), now=datetime(2026, 9, 1, 9, 35, 50))
    third = push_svc.run_due_push(object(), now=datetime(2026, 9, 1, 9, 40, 0))
    assert first is not None and first["ok"]
    assert second is None  # 同一分钟 (misfire 补跑) 只推一次
    assert third is not None  # 下一个对齐点正常推送


def test_run_due_push_enabled_without_url_skips(prefs_dir, monkeypatch):
    monkeypatch.setattr(preferences, "get_webhook_push_schedule",
                        lambda: _cfg(webhook_url=""))
    assert push_svc.run_due_push(object(), now=datetime(2026, 9, 1, 9, 35)) is None


# ---------- 飞书卡片渲染与发送 (format=feishu) ----------

def test_render_board_markdown_units():
    # 字段单位经前端渲染逻辑核实: 指数涨跌幅为百分数; 个股/概念涨跌幅为小数 (x100);
    # 占比/封板率为百分数; 成交额为元。
    snapshot = {
        "as_of": "2026-09-01",
        "data_time": {"realtime": True, "text": "实时 10:35"},
        "overview": {
            "emotion": {"label": "偏冷", "score": 41},
            "indices": [{"symbol": "000001.SH", "name": "上证指数",
                         "last_price": 3456.78, "change_pct": 0.52}],
            "breadth": {"up": 3200, "flat": 500, "down": 1800, "up_pct": 58.2, "avg_pct": 0.0123},
            "limit": {"limit_up": 68, "limit_down": 5, "broken": 12, "seal_rate": 85.0,
                      "max_boards": 6, "tiers": [
                          {"boards": 6, "count": 1,
                           "stocks": [{"symbol": "600519.SH", "name": "贵州茅台", "amount": 9.9}]},
                          {"boards": 4, "count": 3, "stocks": [
                              {"symbol": "1.SH", "name": "股A", "amount": 3.0},
                              {"symbol": "2.SH", "name": "股B", "amount": 2.0},
                              {"symbol": "3.SH", "name": "股C", "amount": 1.0}]},
                          {"boards": 2, "count": 30, "stocks": [
                              {"symbol": f"{i}.SZ", "name": f"股{i}", "amount": 0.1} for i in range(5)]},
                      ]},
            "amount": {"total": 1.2345e12, "avg": 2.5e8},
            "trend": {"above_ma5_pct": 55.5, "above_ma20_pct": 48.0, "above_ma60_pct": 61.2,
                      "new_high": 30, "new_low": 8},
            "top_gainers": [{"symbol": "600519.SH", "name": "贵州茅台", "change_pct": 0.0366}],
            "concept_rank": {"leading": [{"name": "低空经济", "avg_pct": 0.021,
                                          "leaders": [
                                              {"name": "股A", "change_pct": 0.05},
                                              {"name": "股B", "change_pct": 0.04},
                                              {"name": "股C", "change_pct": 0.03}]}], "lagging": []},
            "industry_rank": {"leading": [{"name": "银行", "avg_pct": 0.008,
                                           "leader": {"name": "招行", "change_pct": 0.02}}],
                              "lagging": [{"name": "地产", "avg_pct": -0.012}]},
        },
        "alerts": {"alerts": [{"message": "涨停封板: XX"}], "total": 42},
    }
    md = push_svc.render_board_markdown(snapshot)
    assert "**情绪评分**: 偏冷 41" in md              # 情绪评分行
    assert "上证指数 3456.78 +0.52%" in md    # 指数含点位 + 百分数涨跌幅
    assert "贵州茅台 +3.66%" in md           # 个股为小数, x100
    assert "低空经济 +2.10%(股A/股B/股C)" in md  # 概念含前三龙头
    assert "银行 +0.80%(招行)" in md             # 仅有旧 leader 字段时回退单龙头
    assert "低空经济 +2.10%" in md           # 概念为小数, x100
    assert "**强势行业** 银行 +0.80%" in md   # 行业为小数, x100
    assert "**弱势行业** 地产 -1.20%" in md
    assert "1.23万亿" in md and "2.50亿" in md
    assert "封板率 85%" in md and "最高连板 6板" in md
    # 梯队与首页 LadderMini 一致: ≥2板、梯队内 ≤3 只展示股名, 5 只不展示
    assert "6板x1 贵州茅台" in md
    assert "4板x3 股A·股B·股C" in md
    assert "2板x30" in md and "股2" not in md.split("梯队")[1].split("\n")[0]
    assert "上涨率 58%" in md
    assert "涨停封板: XX" in md and "42 条" in md


def test_render_board_markdown_empty_is_safe():
    assert push_svc.render_board_markdown({}) == "看板暂无数据"
    assert push_svc.render_board_markdown({"overview": {}, "alerts": {}}) == "看板暂无数据"


def test_render_board_text_plain():
    snapshot = {
        "overview": {
            "indices": [{"symbol": "000001.SH", "name": "上证指数",
                         "last_price": 3456.78, "change_pct": 0.52}],
            "top_gainers": [{"symbol": "600519.SH", "name": "贵州茅台", "change_pct": 0.0366}],
        },
        "alerts": {"alerts": [], "total": 0},
    }
    txt = push_svc.render_board_text(snapshot)
    assert "**" not in txt  # 纯文本版无 markdown 加粗标记
    assert "上证指数 3456.78 +0.52%" in txt
    assert "贵州茅台 +3.66%" in txt


def test_render_board_markdown_table():
    snapshot = {
        "as_of": "2026-09-01",
        "data_time": {"realtime": True, "text": "实时 10:35"},
        "overview": {
            "emotion": {"label": "偏冷", "score": 41},
            "indices": [{"symbol": "000001.SH", "name": "上证指数",
                         "last_price": 3456.78, "change_pct": 0.52}],
            "breadth": {"up": 3200, "flat": 500, "down": 1800, "up_pct": 58.2, "avg_pct": 0.0123},
            "limit": {"limit_up": 68, "limit_down": 5, "broken": 12, "seal_rate": 85.0,
                      "max_boards": 6, "tiers": [
                          {"boards": 6, "count": 1,
                           "stocks": [{"symbol": "600519.SH", "name": "贵州茅台", "amount": 9.9}]},
                          {"boards": 2, "count": 30, "stocks": [
                              {"symbol": f"{i}.SZ", "name": f"股{i}", "amount": 0.1} for i in range(5)]},
                      ]},
            "amount": {"total": 1.2345e12, "avg": 2.5e8},
            "top_gainers": [{"symbol": "600519.SH", "name": "贵州茅台", "change_pct": 0.0366}],
            "top_losers": [{"symbol": "5.SH", "name": "某退股", "change_pct": -0.098}],
            "concept_rank": {"leading": [{"name": "低空经济", "avg_pct": 0.021,
                                          "leaders": [
                                              {"name": "股X", "change_pct": 0.05},
                                              {"name": "股Y", "change_pct": 0.04},
                                              {"name": "股Z", "change_pct": 0.03}]}], "lagging": []},
            "industry_rank": {"leading": [{"name": "银行", "avg_pct": 0.008,
                                           "leader": {"name": "招行", "change_pct": 0.02}}],
                              "lagging": [{"name": "地产", "avg_pct": -0.012}]},
        },
        "alerts": {"alerts": [{"message": "涨停封板: XX"}], "total": 42},
    }
    md = push_svc.render_board_markdown_table(snapshot)
    assert "**情绪评分**: 偏冷 41" in md
    assert "**市场概览**" in md and "| 涨/平/跌 | 3200 / 500 / 1800(上涨率 58%) |" in md
    assert "**指数**" in md and "| 上证指数 | 3456.78 | +0.52% |" in md
    assert "**涨停梯队**" in md
    assert "| 6板 | 1 | 贵州茅台 |" in md
    assert "| 2板 | 30 | 股0、股1、股2 |" in md   # 每档展示成交额前 3 的股名
    assert "| 贵州茅台 | +3.66% | 某退股 | -9.80% |" in md  # 涨幅/跌幅合并四列表
    assert "**涨跌幅榜**" in md                              # 合并表带节标题
    assert "| 股票 | 涨跌幅 | 股票 | 涨跌幅 |" in md          # 表头即列标签, 无重复标题行
    assert md.index("**涨跌幅榜**") < md.index("| 股票 | 涨跌幅 | 股票 | 涨跌幅 |")
    assert md.index("- 涨停封板: XX") < md.index("| 股票 | 涨跌幅 | 股票 | 涨跌幅 |")  # 榜单置于最后
    assert "| 低空经济 | +2.10% | 股X、股Y、股Z |" in md   # 概念龙头列
    assert "| 银行 | +0.80% | 招行 |" in md       # 仅有旧 leader 字段时回退单龙头
    assert "| 地产 | -1.20% | — |" in md
    assert "- 涨停封板: XX" in md and "42 条" in md
    assert md.count("| ---") >= 6                 # 表格分隔行齐全

    assert push_svc.render_board_markdown_table({}) == "看板暂无数据"


def test_push_now_feishu_format(prefs_dir, monkeypatch):
    feishu_url = "https://open.feishu.cn/open-apis/bot/v2/hook/abc"
    snapshot = {"as_of": "2026-09-01", "overview": {"emotion": {"label": "回暖", "score": 62}}}
    push_svc.set_snapshot_builder(lambda state: snapshot)
    calls = {}
    monkeypatch.setattr(
        webhook_adapter, "send_feishu_card",
        lambda url, title, subtitle, body_md, secret="": calls.update(
            url=url, title=title, subtitle=subtitle, body=body_md, secret=secret) or True,
    )
    result = push_svc.push_now(
        object(), cfg=_cfg(format="feishu", webhook_url=feishu_url, feishu_secret="sec"))
    assert result["ok"] is True
    assert calls["url"] == feishu_url
    assert calls["title"] == "市场看板快照"
    assert "2026-09-01" in calls["subtitle"] and "回暖" in calls["subtitle"]
    assert calls["secret"] == "sec"
    assert "**情绪评分**: 回暖 62" in calls["body"] or "涨跌" in calls["body"]
    status = push_svc.get_push_status()
    assert status["last_ok"] is True
    assert status["last_format"] == "feishu"  # 状态记录实际使用的格式, 便于排查格式错配

    # 飞书格式但地址非飞书 → 失败并计入状态
    bad = push_svc.push_now(
        object(), cfg=_cfg(format="feishu", webhook_url="https://example.com/hook"))
    assert bad["ok"] is False
    assert "飞书" in bad["detail"]
    assert push_svc.get_push_status()["last_ok"] is False


def test_push_now_kol_format(prefs_dir, monkeypatch):
    """KOL Webhook 简化格式: {"text","title","msg_id"} + 可选飞书同款签名。"""
    kol_url = "https://vpush.example.com/api/kol-webhook/tok"
    snapshot = {
        "as_of": "2026-09-01",
        "generated_at": "2026-09-01T10:35:00+08:00",
        "overview": {"emotion": {"label": "回暖", "score": 62}, "breadth": {"up": 1}},
        "alerts": {"alerts": [], "total": 0},
    }
    push_svc.set_snapshot_builder(lambda state: snapshot)
    calls = {}
    monkeypatch.setattr(
        webhook_adapter, "send_generic_webhook",
        lambda url, payload: calls.update(url=url, payload=payload) or (True, "HTTP 200"),
    )

    result = push_svc.push_now(
        object(), cfg=_cfg(format="kol", webhook_url=kol_url, feishu_secret="sec"))
    assert result["ok"] is True
    assert calls["url"] == kol_url
    body = calls["payload"]
    assert body["title"].startswith("市场看板快照 2026-09-01")
    assert "回暖" in body["title"]
    assert "**市场概览**" in body["text"]        # markdown 表格版摘要
    assert "| 涨/平/跌 |" in body["text"]
    assert body["msg_id"] == "tickflow-board-2026-09-01-1035"  # 幂等键: 交易日+触发分钟
    assert len(body["text"]) <= 8000
    # 签名算法与飞书一致: sign = base64(hmac_sha256(key=f"{ts}\n{secret}", msg=空))
    expected_sign = base64.b64encode(hmac.new(
        f"{body['timestamp']}\nsec".encode(), digestmod=hashlib.sha256).digest()).decode()
    assert body["sign"] == expected_sign

    # 未配置密钥 → 不带 timestamp/sign
    calls.clear()
    push_svc.push_now(object(), cfg=_cfg(format="kol", webhook_url=kol_url))
    body2 = calls["payload"]
    assert "timestamp" not in body2 and "sign" not in body2
    assert body2["msg_id"] == "tickflow-board-2026-09-01-1035"
    assert push_svc.get_push_status()["last_format"] == "kol"


# ---------- 看板快照结构 ----------

def test_build_board_snapshot_structure(tmp_path, monkeypatch, prefs_dir):
    fake_overview = {"as_of": "2026-09-01", "emotion": {"score": 60}}
    monkeypatch.setattr(
        overview_api, "market_overview_cached",
        lambda state, as_of=None: fake_overview,
    )
    # 隔离 data.py 的表统计全局缓存, 避免污染其他测试
    from app.api import data as data_api
    monkeypatch.setattr(data_api, "_table_cache", {})
    monkeypatch.setattr(data_api, "_table_cache_ts", {})

    class _Capset:
        def to_dict(self):
            return {"depth5.batch": {"enabled": True}}

    state = SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
        capabilities=_Capset(),
    )
    snap = overview_api.build_board_snapshot(state)

    assert snap["schema_version"] == overview_api.SNAPSHOT_SCHEMA_VERSION
    assert snap["as_of"] == "2026-09-01"
    assert snap["overview"] is fake_overview
    assert snap["alerts"] == {"alerts": [], "total": 0}
    assert set(snap["data_status"]) == {"enriched", "daily"}
    assert snap["generated_at"]
    # 页面门控/引导字段: capabilities 与 settings 块 (v2 起内聚)
    assert snap["capabilities"]["capabilities"]["depth5.batch"]["enabled"] is True
    assert "label" in snap["capabilities"]
    assert snap["settings"]["mode"] in {"none", "free", "api_key"}
    assert snap["settings"]["onboarding_completed"] is False
    # 无 quote_status (非实时) → 数据时点标为交易日收盘
    assert snap["data_time"] == {"realtime": False, "text": "2026-09-01 收盘"}


def test_snapshot_data_time_realtime_vs_close():
    import time as time_mod

    now_ms = time_mod.time() * 1000
    # 盘中: 行情运行中 + 交易时段 + 数据新鲜 → 实时 (最近拉取时间)
    ov = {"as_of": "2026-09-02",
          "quote_status": {"running": True, "is_trading_hours": True,
                           "last_fetch_ms": now_ms - 3000}}
    dt = overview_api._snapshot_data_time(ov)
    assert dt["realtime"] is True
    assert dt["text"].startswith("实时 ")

    # 行情数据陈旧 (>10 分钟) → 按收盘处理
    ov["quote_status"]["last_fetch_ms"] = now_ms - 3600_000
    assert overview_api._snapshot_data_time(ov) == {"realtime": False, "text": "2026-09-02 收盘"}

    # 盘后 (非交易时段) → 收盘, 即使行情服务仍在运行
    ov["quote_status"] = {"running": True, "is_trading_hours": False,
                          "last_fetch_ms": now_ms - 3000}
    assert overview_api._snapshot_data_time(ov) == {"realtime": False, "text": "2026-09-02 收盘"}


# ---------- settings API ----------

def test_settings_api_webhook_push_schedule_roundtrip(prefs_dir):
    saved = settings_api.update_webhook_push_schedule(
        settings_api.WebhookPushScheduleIn(
            enabled=True, webhook_url="https://example.com/hook",
            windows=[{"start_time": "09:15", "end_time": "11:35", "interval_minutes": 10},
                     {"start_time": "13:05", "end_time": "15:05", "interval_minutes": 20}],
        ))
    body = saved["webhook_push_schedule"]
    assert body["enabled"] is True
    assert body["windows"][0] == {"start_time": "09:15", "end_time": "11:35",
                                  "interval_minutes": 10}
    assert body["windows"][1] == {"start_time": "13:05", "end_time": "15:05",
                                  "interval_minutes": 20}
    assert body["format"] == "generic"

    prefs = settings_api.get_preferences()
    assert prefs["webhook_push_schedule"]["windows"] == body["windows"]

    with pytest.raises(HTTPException) as ei:  # 启用但 URL 为空
        settings_api.update_webhook_push_schedule(
            settings_api.WebhookPushScheduleIn(
                enabled=True, webhook_url="",
                windows=[{"start_time": "09:30", "end_time": "11:30",
                          "interval_minutes": 5}]))
    assert ei.value.status_code == 400

    with pytest.raises(HTTPException) as ei2:  # 窗口倒置
        settings_api.update_webhook_push_schedule(
            settings_api.WebhookPushScheduleIn(
                enabled=False, webhook_url="",
                windows=[{"start_time": "09:30", "end_time": "09:00",
                          "interval_minutes": 5}]))
    assert ei2.value.status_code == 400

    with pytest.raises(HTTPException) as ei3:  # 半窗
        settings_api.update_webhook_push_schedule(
            settings_api.WebhookPushScheduleIn(
                enabled=False, webhook_url="",
                windows=[{"start_time": "09:30", "end_time": "11:30",
                          "interval_minutes": 5},
                         {"start_time": "13:00", "end_time": "",
                          "interval_minutes": 5}]))
    assert ei3.value.status_code == 400

    with pytest.raises(ValidationError) as ei4:  # 空窗列表 → pydantic 422
        settings_api.update_webhook_push_schedule(
            settings_api.WebhookPushScheduleIn(
                enabled=False, webhook_url="", windows=[]))
    assert "at least 1 item" in str(ei4.value)

    with pytest.raises(HTTPException) as ei5:  # 飞书格式但地址非飞书
        settings_api.update_webhook_push_schedule(
            settings_api.WebhookPushScheduleIn(
                enabled=True, format="feishu", webhook_url="https://example.com/hook",
                windows=[{"start_time": "09:30", "end_time": "11:30",
                          "interval_minutes": 5}]))
    assert ei5.value.status_code == 400


def test_settings_api_test_push_endpoint(prefs_dir, monkeypatch):
    snapshot = {"as_of": "2026-09-01"}
    push_svc.set_snapshot_builder(lambda state: snapshot)
    sent = {}
    monkeypatch.setattr(
        webhook_adapter, "send_generic_webhook",
        lambda url, payload: sent.update(url=url, payload=payload) or (True, "HTTP 200"),
    )
    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=prefs_dir)))
    request = SimpleNamespace(app=SimpleNamespace(state=state))

    result = settings_api.test_webhook_push(
        settings_api.WebhookPushTestIn(webhook_url="https://t.example/hook"), request)
    assert result["ok"] is True
    assert sent["url"] == "https://t.example/hook"
    assert sent["payload"] is snapshot

    # 传 format → 按表单草稿格式发送 (kol): 载荷为简化格式而非原始快照, 草稿密钥参与签名
    sent.clear()
    settings_api.test_webhook_push(
        settings_api.WebhookPushTestIn(
            webhook_url="https://t.example/hook", format="kol", feishu_secret="sec"), request)
    body = sent["payload"]
    assert body["title"].startswith("市场看板快照 2026-09-01")
    assert body["text"] and body["msg_id"].startswith("tickflow-board-2026-09-01-")
    assert body["sign"]  # 草稿密钥参与签名
    assert push_svc.get_push_status()["last_format"] == "kol"

    # 请求体与已保存配置都没有地址 → 400
    with pytest.raises(HTTPException) as ei:
        settings_api.test_webhook_push(settings_api.WebhookPushTestIn(), request)
    assert ei.value.status_code == 400


def test_settings_api_status_endpoint_reflects_push_result(prefs_dir, monkeypatch):
    push_svc.set_snapshot_builder(lambda state: {"as_of": "2026-09-01"})
    monkeypatch.setattr(
        webhook_adapter, "send_generic_webhook", lambda url, payload: (False, "HTTP 500: x"))
    push_svc.push_now(object(), webhook_url="https://example.com/hook")

    status = settings_api.webhook_push_status()
    assert status["last_ok"] is False
    assert "500" in status["last_detail"]
    assert status["recent"]


# ---------- builder: 概念/行业龙头 ----------

def test_dimension_rank_includes_top3_leaders(monkeypatch, tmp_path):
    """板块条目带涨幅前三龙头 (leaders); 旧 leader 字段保持兼容。"""
    from app.services import market_overview_builder as mob

    quotes = [
        {"symbol": "1.SH", "name": "龙头一", "change_pct": 0.052, "amount": 3e8},
        {"symbol": "2.SH", "name": "龙头二", "change_pct": 0.031, "amount": 2e8},
        {"symbol": "3.SZ", "name": "龙头三", "change_pct": 0.02, "amount": 1e8},
        {"symbol": "4.SZ", "name": "跟风股", "change_pct": -0.01, "amount": 1e8},
    ]
    ext_rows = [{"概念": "低空经济", "symbol": s} for s in ("1.SH", "2.SH", "3.SZ", "4.SZ")]

    class _Store:
        def __init__(self, data_dir):
            pass

        def load_all(self):
            return [SimpleNamespace(id="c1", mode="static",
                                    fields=[SimpleNamespace(name="概念", label="概念")],
                                    symbol_map=None, code_map=None)]

    monkeypatch.setattr(mob, "ExtConfigStore", _Store)
    monkeypatch.setattr(mob, "_read_ext_rows", lambda data_dir, config, field: ext_rows)

    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    out = mob._dimension_rank(quotes, repo, "concept")
    item = out["leading"][0]
    assert item["name"] == "低空经济"
    assert [ld["name"] for ld in item["leaders"]] == ["龙头一", "龙头二", "龙头三"]
    assert all(ld["change_pct"] is not None for ld in item["leaders"])
    assert item["leader"]["name"] == "龙头一"   # 旧字段保留, 看板页兼容


# ---------- 调度注册 ----------

def test_register_push_job_registers_coroutine_cron_job():
    class FakeScheduler:
        def __init__(self):
            self.calls = []

        def add_job(self, fn, **kwargs):
            self.calls.append((fn, kwargs))

    sched = FakeScheduler()
    push_svc.register_push_job(sched, object())

    fn, kwargs = sched.calls[0]
    assert inspect.iscoroutinefunction(fn)  # 协程函数对象直传, 不能用 lambda 包裹
    assert kwargs["id"] == push_svc.PUSH_JOB_ID
    assert kwargs["misfire_grace_time"] == 30
    assert kwargs["max_instances"] == 1
    assert kwargs["replace_existing"] is True
    assert "Asia/Shanghai" in str(kwargs["trigger"].timezone)
    assert "minute='*'" in str(kwargs["trigger"])
