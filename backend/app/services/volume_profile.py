"""全市场日内成交量分布 (volume profile) — 情绪量能维的盘中校准。

从 kline_minute 历史分区构建「到当日第 t 分钟的全市场累计成交量占全天比例」
分布曲线 D(t)。情绪量能维用它把盘中量比从线性折算 (乘 240/elapsed, 隐含日内
均匀分布假设) 校准为历史同时段对比口径 (除以 D(t)), 消除 A 股 U 型日内分布
(早盘/尾盘放量) 的系统性偏差。

范围: 只用于看板/实时情绪的量能维聚合。个股 vol_ratio_5d 及放量信号
(signal_volume_surge) 保持同花顺标准量比口径, 不经本模块。

降级: 历史分钟分区缺失/为空时返回 None, 调用方回退线性折算, 不阻断主流程。
分钟K datetime 的墙钟口径不跨版本稳定 (naive 北京时间/UTC 都出现过), 曲线
一律按「当日第 k 根分钟K」的序号位置对齐, 不依赖墙钟。

缓存: 模块级, 键为 (data_dir, target_date)。盘中只会写入当日分区, 而当日
分区被排除, 历史分区集合在一天内不变, 无需 mtime 失效。
"""
from __future__ import annotations

import logging
import statistics
import threading
from datetime import date
from itertools import pairwise
from pathlib import Path

import polars as pl

from app.market_time import trading_minutes_elapsed, trading_minutes_elapsed_from_ts

logger = logging.getLogger(__name__)

# 分布曲线回看的最近交易日数 (不含当日)。
PROFILE_LOOKBACK_DAYS = 5
# 曲线粒度: 当日分钟K按序号等分为 48 段 (~5 分钟), 跨日直接平均。
PROFILE_BUCKETS = 48
_TRADING_MINUTES = 240.0

_lock = threading.Lock()
_profile_cache: dict[tuple[Path, date], list[float] | None] = {}


def _profile_partitions(data_dir: Path, target_date: date) -> list[Path]:
    """最近 PROFILE_LOOKBACK_DAYS 个早于 target_date 的分钟分区 (旧→新)。"""
    root = data_dir / "kline_minute"
    if not root.exists():
        return []
    parts: list[tuple[date, Path]] = []
    for d in root.glob("date=*"):
        try:
            part_date = date.fromisoformat(d.name.replace("date=", ""))
        except ValueError:
            continue
        if part_date < target_date and (d / "part.parquet").exists():
            parts.append((part_date, d / "part.parquet"))
    return [p for _, p in sorted(parts)[-PROFILE_LOOKBACK_DAYS:]]


def _day_cum_curve(part: Path) -> list[float] | None:
    """单日曲线: 48 段等分的累计成交量占全天比例 (段末值), 空数据返回 None。"""
    try:
        df = pl.read_parquet(part, columns=["datetime", "volume"])
    except Exception as e:
        logger.warning("volume profile read %s failed: %s", part, e)
        return None
    if df.is_empty():
        return None
    total = df["volume"].sum()
    if not total or total <= 0:
        return None
    ranked = (
        df.sort("datetime")
        .with_row_index("_idx")
        .with_columns(
            (pl.col("_idx") * PROFILE_BUCKETS // df.height).alias("_bucket"),
        )
        .group_by("_bucket")
        .agg(pl.col("volume").sum())
        .sort("_bucket")
    )
    cum = ranked["volume"].cum_sum() / total
    # 不足 48 段 (标的极少/数据缺失) 时尾部段缺失, 按最后已知值外推到全天。
    curve = [1.0] * PROFILE_BUCKETS
    values = cum.to_list()
    for i, v in enumerate(values):
        curve[i] = v
    return curve


def get_market_volume_profile(data_dir: Path, target_date: date) -> list[float] | None:
    """返回 48 点累计量分布 D(t) (0~1, 末点=1); 无历史分钟数据返回 None。"""
    key = (data_dir, target_date)
    with _lock:
        if key in _profile_cache:
            return _profile_cache[key]

    curve: list[float] | None = None
    parts = _profile_partitions(data_dir, target_date)
    days = [c for p in parts if (c := _day_cum_curve(p)) is not None]
    if days:
        n = len(days)
        curve = [sum(d[i] for d in days) / n for i in range(PROFILE_BUCKETS)]
        # 防御: 曲线必须单调非降且末点为 1 (坏数据时放弃校准走线性回退)。
        if curve[-1] < 0.99 or any(b < a - 1e-9 for a, b in pairwise(curve)):
            logger.warning("volume profile curve invalid, fallback to linear")
            curve = None

    with _lock:
        _profile_cache[key] = curve
    return curve


def cum_ratio_at(profile: list[float] | None, elapsed_minutes: float) -> float | None:
    """历史分布下「已交易 elapsed 分钟」时点的全天累计量占比 D(t)。

    elapsed 落在段中间时取所在段末值 (折算偏保守)。不可用返回 None。
    """
    if not profile or elapsed_minutes is None or elapsed_minutes <= 0:
        return None
    pos = elapsed_minutes / _TRADING_MINUTES * PROFILE_BUCKETS
    idx = min(PROFILE_BUCKETS - 1, int(pos))
    return profile[idx]


def calibration_factor(
    data_dir: Path,
    target_date: date,
    elapsed_minutes: float,
) -> float | None:
    """情绪量能维的量比折算因子 1/D(t); 曲线不可用返回 None (调用方回退线性)。"""
    profile = get_market_volume_profile(data_dir, target_date)
    if profile is None:
        return None
    ratio = cum_ratio_at(profile, elapsed_minutes)
    if not ratio or ratio <= 0:
        return None
    return 1.0 / ratio


def elapsed_minutes_from_quote_ts(quote_ts_values) -> float | None:
    """今日行 quote_ts (毫秒) 的中位数 → 已交易分钟数; 全缺失返回 None。

    与看板增量路径 (quote_service._flush_live_enriched) 的取数口径一致:
    行情真实成交时间优先于服务端时间。
    """
    valid = [v for v in quote_ts_values if v]
    if not valid:
        return None
    return trading_minutes_elapsed_from_ts(statistics.median(valid))


def today_elapsed_minutes(quote_ts_values) -> float:
    """已交易分钟数: quote_ts 中位数优先, 服务端时间兜底 (永远有值)。"""
    elapsed = elapsed_minutes_from_quote_ts(quote_ts_values)
    return elapsed if elapsed is not None else trading_minutes_elapsed()
