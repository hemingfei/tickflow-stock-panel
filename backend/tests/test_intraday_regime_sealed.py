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


# ───────────────────────── 实时环境 impl 连板口径 ─────────────────────────


def _limit_up_frame(target_date):
    """3 个交易日 × 2 只主板股: A 连续两日 +10% 涨停 (前两日无前收基准),
    B 持平平盘。raw 与复权价一致 (无除权), 涨停由理论价推导命中。"""
    from datetime import timedelta

    import polars as pl

    d3 = target_date
    d2 = d3 - timedelta(days=1)
    d1 = d3 - timedelta(days=2)

    rows = []
    # A: 10.0 → 11.0 → 12.1, 每日恰 +10% (主板涨停), 后两日 signal_limit_up=True
    for d, close in ((d1, 10.0), (d2, 11.0), (d3, 12.1)):
        rows.append({
            "symbol": "600001.SH", "date": d,
            "open": close, "high": close, "low": close, "close": close,
            "raw_close": close, "raw_high": close, "raw_low": close,
            "volume": 1000.0, "amount": close * 1000.0,
        })
    # B: 平盘
    for d in (d1, d2, d3):
        rows.append({
            "symbol": "600002.SH", "date": d,
            "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
            "raw_close": 10.0, "raw_high": 10.0, "raw_low": 10.0,
            "volume": 1000.0, "amount": 10000.0,
        })
    return pl.DataFrame(rows)


def _regime_repo(tmp_path, target_date):
    from types import SimpleNamespace

    import polars as pl

    enriched_dir = tmp_path / "kline_daily_enriched"
    from datetime import timedelta

    for d in (target_date - timedelta(days=2), target_date - timedelta(days=1), target_date):
        part = enriched_dir / f"date={d.isoformat()}" / "part.parquet"
        part.parent.mkdir(parents=True, exist_ok=True)
        day = _limit_up_frame(target_date).filter(pl.col("date") == d)
        day.write_parquet(part)

    instruments = pl.DataFrame({"symbol": ["600001.SH", "600002.SH"], "name": ["甲", "乙"]})
    return SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_instruments=lambda: instruments,
        get_historical_shares=lambda: None,
    )


def test_intraday_regime_impl_counts_consecutive_limit_ups(tmp_path, monkeypatch):
    """实时环境链路必须产出 consecutive_limit_ups。

    compute_limit_signals 的 needed 若漏传该列, _aggregate_daily 的最高连板/
    梯队指标全被跳过 → max_consecutive 恒 0, 投机维的连板高度子项恒 0 分
    (与看板/情绪口径失真)。"""
    from datetime import datetime

    from app.market_time import CN_TZ
    from app.services import intraday_regime as ir

    fixed = datetime(2026, 9, 3, 10, 0, tzinfo=CN_TZ)
    monkeypatch.setattr(ir, "cn_now", lambda: fixed)
    repo = _regime_repo(tmp_path, fixed.date())

    record = ir._compute_intraday_regime_impl(repo, None)
    assert record is not None
    assert record["limit_up"] == 1
    # 核心断言: 连板数被正确聚合 (600001.SH 连续 2 日涨停)
    assert record["max_consecutive"] == 2


def test_intraday_regime_impl_skips_when_today_partition_missing(tmp_path, monkeypatch):
    """今日 enriched 分区未生成 (盘前/竞价/节假日) → 直接 None, 不做全市场重算。"""
    from datetime import datetime

    from app.market_time import CN_TZ
    from app.services import intraday_regime as ir

    fixed = datetime(2026, 9, 3, 10, 0, tzinfo=CN_TZ)
    monkeypatch.setattr(ir, "cn_now", lambda: fixed)
    repo = _regime_repo(tmp_path, fixed.date())
    # 删掉今日分区, 只留历史
    import shutil
    shutil.rmtree(tmp_path / "kline_daily_enriched" / f"date={fixed.date().isoformat()}")

    assert ir._compute_intraday_regime_impl(repo, None) is None


def test_intraday_sentiment_impl_record_content_preserved(tmp_path, monkeypatch):
    """实时情绪链路 needed 收紧后记录内容不变: limit_up/max_boards/tier2_count
    与评分字段仍正确产出 (五档 depth 未注入 → 假涨停不修正)。"""
    from datetime import datetime

    from app.market_time import CN_TZ
    from app.services import intraday_sentiment as isl

    fixed = datetime(2026, 9, 3, 10, 0, tzinfo=CN_TZ)
    monkeypatch.setattr(isl, "cn_now", lambda: fixed)

    from datetime import timedelta
    from types import SimpleNamespace

    import polars as pl

    target_date = fixed.date()
    enriched_dir = tmp_path / "kline_daily_enriched"
    frame = _limit_up_frame(target_date)
    for d in (target_date - timedelta(days=2), target_date - timedelta(days=1), target_date):
        part = enriched_dir / f"date={d.isoformat()}" / "part.parquet"
        part.parent.mkdir(parents=True, exist_ok=True)
        frame.filter(pl.col("date") == d).write_parquet(part)

    instruments = pl.DataFrame({"symbol": ["600001.SH", "600002.SH"], "name": ["甲", "乙"]})
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_instruments=lambda: instruments,
        get_historical_shares=lambda: None,
    )

    record = isl._compute_intraday_sentiment_impl(repo, None, None)
    assert record is not None
    # 涨停/连板/梯队口径 (600001.SH 连续 2 板 → tier2 计 1 只)
    assert record["limit_up"] == 1
    assert record["max_boards"] == 2
    assert record["tier2_count"] == 1
    # 六维 + 总分齐全 (0-100), 标签为中文
    for key in ("index_score", "profit_score", "money_score", "speculation_score",
                "resilience_score", "mainline_score", "emotion_score"):
        assert isinstance(record[key], int) and 0 <= record[key] <= 100
    assert record["emotion_label"] in {"强势", "偏暖", "震荡", "偏冷", "冰点"}
    # 无主线成分 (ExtConfigStore 为空) → 主线维 50 分 (与看板空档语义一致)
    assert record["mainline_score"] == 50
