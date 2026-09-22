"""实时行情开关重启持久化回归。

进程关闭 (main.py lifespan shutdown) 调 qs.stop(), 旧实现无条件把
realtime_quotes_enabled 写成 False, 覆盖用户上次的开启状态; 重启后
boot_check 读到 False, 实时行情总是关闭。修复后进程关闭只停线程
(stop() 不再写 preferences), 开关以用户在设置页的选择为准;
用户主动关闭仍走 disable() 持久化为 False。
"""
from __future__ import annotations

import pytest

from app.services import preferences
from app.services.quote_service import QuoteService


@pytest.fixture
def pref_store(monkeypatch):
    """内存版 preferences: 隔离真实 data_dir, 并记录 save 调用。"""
    store: dict = {}
    saves: list[dict] = []

    def _load() -> dict:
        return dict(store)

    def _save(updates: dict) -> dict:
        store.update(updates)
        saves.append(dict(updates))
        return dict(store)

    monkeypatch.setattr(preferences, "load", _load)
    monkeypatch.setattr(preferences, "save", _save)
    return store, saves


@pytest.fixture
def allow_realtime(monkeypatch):
    monkeypatch.setattr(QuoteService, "is_realtime_allowed", classmethod(lambda cls: True))


@pytest.fixture(autouse=True)
def no_poll_thread(monkeypatch):
    """轮询循环替换为空操作: 测试只验证开关生命周期, 不真正拉行情。"""
    monkeypatch.setattr(QuoteService, "_poll_loop", lambda self: None)


def test_shutdown_stop_keeps_enabled_preference(pref_store, allow_realtime):
    """进程关闭路径: 停线程但不把开关偏好覆盖为关闭。"""
    store, saves = pref_store
    qs = QuoteService()
    assert qs.enable() is True
    assert store["realtime_quotes_enabled"] is True

    qs.stop()  # main.py lifespan shutdown 路径

    assert qs._running is False
    assert qs._enabled is False
    # 旧实现在此写 False → 重启后开关总是关闭 (本次修复的问题)
    assert store["realtime_quotes_enabled"] is True
    assert {"realtime_quotes_enabled": False} not in saves


def test_restart_restores_enabled_state(pref_store, allow_realtime):
    """模拟重启: 开启 → 进程关闭 → 新实例 boot_check 恢复开启。"""
    store, _ = pref_store
    qs = QuoteService()
    assert qs.enable() is True
    qs.stop()

    rebooted = QuoteService()
    rebooted.boot_check()

    assert rebooted._running is True
    assert rebooted._enabled is True
    assert store["realtime_quotes_enabled"] is True


def test_user_disable_persists_off(pref_store, allow_realtime):
    """用户主动关闭: 偏好持久化为 False, 重启后保持关闭。"""
    store, _ = pref_store
    qs = QuoteService()
    assert qs.enable() is True
    qs.disable()
    assert store["realtime_quotes_enabled"] is False

    rebooted = QuoteService()
    rebooted.boot_check()

    assert rebooted._running is False
    assert rebooted._enabled is False


def test_boot_check_denied_tier_resets_preference(pref_store, monkeypatch):
    """无实时权限时 boot_check 把 enabled 偏好回落为 False (UI 不误显示开启)。"""
    store, _ = pref_store
    monkeypatch.setattr(QuoteService, "is_realtime_allowed", classmethod(lambda cls: False))
    store["realtime_quotes_enabled"] = True

    qs = QuoteService()
    qs.boot_check()

    assert qs._running is False
    assert store["realtime_quotes_enabled"] is False
