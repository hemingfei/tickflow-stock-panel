"""全市场日内成交量分布 (volume_profile) 测试。

验证: 曲线构建 (序号对齐, 不依赖墙钟)、当日分区排除、U 型分布的校准效果、
缺失回退、看板与实时情绪两边折算的数学等价。
"""
from __future__ import annotations

import math
from datetime import date, datetime
from itertools import pairwise
from pathlib import Path

import polars as pl

from app.market_time import trading_minutes_elapsed_from_dt
from app.services import volume_profile as vp

TODAY = date(2026, 9, 3)


def _write_minute_partition(data_dir: Path, day: str, rows: list[tuple[str, datetime, float]]) -> None:
    part = data_dir / "kline_minute" / f"date={day}" / "part.parquet"
    part.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": [r[0] for r in rows],
        "datetime": [r[1] for r in rows],
        "volume": [r[2] for r in rows],
    }).write_parquet(part)


def _utc(h: int, m: int) -> datetime:
    """分钟分区 datetime 墙钟口径不跨版本稳定, 测试用任意固定时区值,
    曲线按序号对齐不受影响。"""
    return datetime(2026, 9, 2, h, m)


def _mk_day_rows(volumes_by_step: list[float]) -> list[tuple[str, datetime, float]]:
    """单日 240 根分钟K: 两只标的, 前后两半各 120 根。"""
    rows = []
    for i, v in enumerate(volumes_by_step):
        h = 1 + i // 60
        m = i % 60
        rows.append(("000001.SZ", _utc(h, m), v))
        rows.append(("000002.SZ", _utc(h, m), v))
    return rows


def test_curve_captures_u_shape_and_excludes_today(tmp_path: Path) -> None:
    """U 型分布 (开盘/尾盘占 30%, 中段占 5%): 曲线早盘显著高于线性比例;
    当日分区不参与构建。"""
    data_dir = tmp_path / "data"
    # 历史日: 开盘 30% + 中段 40% + 尾盘 30% → 到第 60 根时已 ~30%
    heavy_ends = [30.0] * 10 + [5.0] * 220 + [30.0] * 10
    _write_minute_partition(data_dir, "2026-09-01", _mk_day_rows(heavy_ends))
    # 当日分区 (必须被排除): 全部量堆在前面
    _write_minute_partition(data_dir, "2026-09-03", _mk_day_rows([100.0] * 240))

    profile = vp.get_market_volume_profile(data_dir, TODAY)

    assert profile is not None
    assert len(profile) == vp.PROFILE_BUCKETS
    assert math.isclose(profile[-1], 1.0)
    assert all(b >= a - 1e-9 for a, b in pairwise(profile))
    # U 型: 前 ~1/12 段 (对应开盘) 累计比例远超线性 1/48
    assert profile[3] > 3.0 / vp.PROFILE_BUCKETS


def test_missing_partitions_returns_none(tmp_path: Path) -> None:
    """无历史分钟分区: 曲线与校准因子均为 None (调用方回退线性)。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    assert vp.get_market_volume_profile(data_dir, TODAY) is None
    assert vp.calibration_factor(data_dir, TODAY, 60.0) is None


def test_today_only_partition_returns_none(tmp_path: Path) -> None:
    """只有当日分区 (minute_refresh 刚开启): 无历史曲线, 回退线性。"""
    data_dir = tmp_path / "data"
    _write_minute_partition(data_dir, "2026-09-03", _mk_day_rows([10.0] * 240))
    assert vp.get_market_volume_profile(data_dir, TODAY) is None


def test_cum_ratio_at_boundaries(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    heavy_ends = [30.0] * 10 + [5.0] * 220 + [30.0] * 10
    _write_minute_partition(data_dir, "2026-09-01", _mk_day_rows(heavy_ends))
    profile = vp.get_market_volume_profile(data_dir, TODAY)

    assert vp.cum_ratio_at(None, 60.0) is None
    assert vp.cum_ratio_at(profile, 0.0) is None
    # 超界 clamp 到末段
    assert math.isclose(vp.cum_ratio_at(profile, 999.0), 1.0)
    # 开盘 ~25 分钟落在第一段末 (重盘段): 占比远高于线性 25/240
    d = vp.cum_ratio_at(profile, 25.0)
    assert d is not None and d > 25.0 / 240.0


def test_calibration_sides_equivalent(tmp_path: Path) -> None:
    """两边数学等价: 实时情绪 rawx1/D == 看板 (rawx240/e)x(e/240)/D。"""
    data_dir = tmp_path / "data"
    heavy_ends = [30.0] * 10 + [5.0] * 220 + [30.0] * 10
    _write_minute_partition(data_dir, "2026-09-01", _mk_day_rows(heavy_ends))

    elapsed = 60.0
    raw_ratio = 0.8  # 未折算的累计量比
    d = vp.cum_ratio_at(vp.get_market_volume_profile(data_dir, TODAY), elapsed)
    assert d is not None

    intraday_result = raw_ratio * vp.calibration_factor(data_dir, TODAY, elapsed)
    overview_factor = (elapsed / 240.0) / d
    overview_result = raw_ratio * (240.0 / elapsed) * overview_factor
    assert math.isclose(intraday_result, overview_result, rel_tol=1e-9)


def test_elapsed_from_quote_ts_median() -> None:
    """中位数取真实成交时间口径; 全缺失回退 None (由调用方兜底服务端时间)。"""
    ts = [int(datetime(2026, 9, 3, 10, 30).timestamp() * 1000),
          int(datetime(2026, 9, 3, 10, 40).timestamp() * 1000)]
    elapsed = vp.elapsed_minutes_from_quote_ts([*ts, None])
    assert elapsed == trading_minutes_elapsed_from_dt(datetime(2026, 9, 3, 10, 35))
    assert vp.elapsed_minutes_from_quote_ts([None, 0]) is None
