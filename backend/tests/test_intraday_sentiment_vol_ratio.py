"""实时情绪量比时间折算测试。

看板增量路径 (compute_enriched_today) 盘中量比按 elapsed_minutes 折算为全日
等效口径; 实时情绪的 compute_indicators 全量重算路径不折算, 导致盘中量能维
被系统性低估。验证 _apply_vol_ratio_time_factor 的折算与边界 (repo 的
data_dir 无历史分钟分区时走线性回退)。
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import polars as pl

from app.market_time import trading_minutes_elapsed_from_dt
from app.services.intraday_sentiment import (
    _apply_vol_ratio_time_factor,
    _intraday_elapsed_minutes,
)

TODAY = date(2026, 9, 3)


class _EmptyRepo:
    """data_dir 无 kline_minute → 分布曲线不可用 → 线性回退。"""

    class _Store:
        data_dir = Path("__nonexistent_volume_profile_dir__")

    store = _Store()


def _empty_repo() -> _EmptyRepo:
    return _EmptyRepo()


def _mk_today_df(quote_ts_ms: list[int | None]) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [f"{i:06d}.SZ" for i in range(len(quote_ts_ms))],
        "date": [TODAY] * len(quote_ts_ms),
        "vol_ratio_5d": [0.5] * len(quote_ts_ms),
        "quote_ts": quote_ts_ms,
    })


def _ts(h: int, m: int, s: int = 0) -> int:
    """北京时间墙钟 → 毫秒时间戳 (与 quote_ts 存储口径一致)。"""
    return int(datetime(2026, 9, 3, h, m, s).timestamp() * 1000)


def test_fold_at_morning_midway() -> None:
    """上午 10:30 (已交易 60 分钟): 量比 x4 放大到全日等效。"""
    df = _mk_today_df([_ts(10, 30)] * 3)
    out = _apply_vol_ratio_time_factor(_empty_repo(), df, TODAY)
    assert out["vol_ratio_5d"].to_list() == [2.0, 2.0, 2.0]


def test_fold_afternoon_accounts_for_lunch_break() -> None:
    """下午 14:00 (已交易 120+60=180 分钟含午休): 量比 x(240/180)。"""
    df = _mk_today_df([_ts(14, 0)] * 2)
    out = _apply_vol_ratio_time_factor(_empty_repo(), df, TODAY)
    expected = 0.5 * (240.0 / 180.0)
    assert all(abs(v - expected) < 1e-9 for v in out["vol_ratio_5d"].to_list())


def test_no_fold_at_close() -> None:
    """15:00 后 elapsed=240: 已是全天量, 不折算。"""
    df = _mk_today_df([_ts(15, 0)] * 2)
    out = _apply_vol_ratio_time_factor(_empty_repo(), df, TODAY)
    assert out["vol_ratio_5d"].to_list() == [0.5, 0.5]


def test_no_fold_during_auction() -> None:
    """集合竞价 9:25 elapsed=0: 不折算 (与看板 compute_enriched_today 的
    time_factor=1 语义一致)。"""
    df = _mk_today_df([_ts(9, 25)] * 2)
    out = _apply_vol_ratio_time_factor(_empty_repo(), df, TODAY)
    assert out["vol_ratio_5d"].to_list() == [0.5, 0.5]


def test_partial_null_quote_ts_uses_median_of_valid() -> None:
    """部分 quote_ts 缺失: 用有效值的中位数折算 (停牌/无成交行不拖偏)。"""
    df = _mk_today_df([_ts(10, 30), None, _ts(10, 30)])
    out = _apply_vol_ratio_time_factor(_empty_repo(), df, TODAY)
    assert out["vol_ratio_5d"].to_list() == [2.0, 2.0, 2.0]


def test_all_null_quote_ts_falls_back_to_server_time(monkeypatch) -> None:
    """quote_ts 全缺失: 兜底服务端时间 (这里 monkeypatch 到已交易 60 分钟)。"""
    from app.services import volume_profile as vp

    monkeypatch.setattr(vp, "trading_minutes_elapsed", lambda: 60.0)
    df = _mk_today_df([None, None])
    out = _apply_vol_ratio_time_factor(_empty_repo(), df, TODAY)
    assert out["vol_ratio_5d"].to_list() == [2.0, 2.0]


def test_elapsed_helper_reads_quote_ts_median() -> None:
    """_intraday_elapsed_minutes: quote_ts 中位数 → trading_minutes_elapsed_from_ts。"""
    df = _mk_today_df([_ts(10, 30), _ts(10, 40)])
    elapsed = _intraday_elapsed_minutes(df)
    assert elapsed == trading_minutes_elapsed_from_dt(datetime(2026, 9, 3, 10, 35))
