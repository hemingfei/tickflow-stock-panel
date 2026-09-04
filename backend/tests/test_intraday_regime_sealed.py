"""实时环境涨停数封板口径测试。

环境分钟链路此前直接计数 signal_limit_up(摸板数), 而实时情绪/看板用
五档 sealed 修正扣除假涨停(封板数), 导致合并页两条走势图的涨停数不吻合。
验证统一口径的两层: depth 假涨停计数(环境/情绪共用)与环境侧接线 helper。
"""
from __future__ import annotations

from datetime import date

from app.services.depth_service import DepthService
from app.services.intraday_regime import _depth_fake_limit_up

TODAY = date(2026, 9, 3)


# ───────────────────────── depth 假涨停计数 ─────────────────────────


def _depth_svc(up_map: dict, ready: bool) -> DepthService:
    svc = DepthService()
    svc.get_sealed_map = lambda target_date, is_down=False: up_map
    svc.is_sealed_ready = lambda target_date: ready
    return svc


def test_fake_limit_count_ready_counts_unsealed():
    """就绪时只统计 sealed=False 的家数; sealed=None(待确认)不算假涨停。"""
    svc = _depth_svc({
        "A": {"sealed": True},
        "B": {"sealed": False},
        "C": {"sealed": False},
        "D": {"sealed": None},
    }, ready=True)
    assert svc.fake_limit_count(TODAY) == 2


def test_fake_limit_count_not_ready_returns_zero():
    """未就绪 → 0, 调用方语义为不修正(与原 sealed_ready=False 行为一致)。"""
    svc = _depth_svc({"A": {"sealed": False}}, ready=False)
    assert svc.fake_limit_count(TODAY) == 0


def test_fake_limit_count_empty_map_returns_zero():
    svc = _depth_svc({}, ready=True)
    assert svc.fake_limit_count(TODAY) == 0


# ───────────────────────── 环境接线 helper ─────────────────────────


def test_depth_fake_limit_up_none_service_returns_zero():
    """depth 未注入(如服务初始化失败): 不修正, 保持原始摸板口径。"""
    assert _depth_fake_limit_up(None, TODAY) == 0


def test_depth_fake_limit_up_passes_through_ready_count():
    svc = _depth_svc({"A": {"sealed": False}, "B": {"sealed": True}}, ready=True)
    assert _depth_fake_limit_up(svc, TODAY) == 1


def test_depth_fake_limit_up_exception_fails_safe():
    """depth 读取异常: 返回 0 不修正, 不拖垮整条分钟记录。"""

    class _BrokenSvc:
        def fake_limit_count(self, target_date, is_down=False):
            raise RuntimeError("depth unavailable")

    assert _depth_fake_limit_up(_BrokenSvc(), TODAY) == 0
