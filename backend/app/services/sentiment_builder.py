"""情绪周期(sentiment)计算 — 纯函数模块。

职责: 从已算好的 enriched 数据(含信号列)按日聚合情绪指标,
用与 market_overview_builder 完全相同的逻辑计算 6 维度分 + 总情绪分,
持久化为时序表。不重算指标(不走 compute_indicators), 不依赖 quote/depth service。

设计原则:
- 每个历史日期只用该日期当天的数据, 不依赖未来数据
- 完全复用 market_overview_builder 的评分逻辑, 确保与看板一致
- 存储所有计算所需的原始指标, 而非仅 6 个维度分
- 性能优化: 一次性加载完整日期范围 + warmup, 批量计算指标, 最后逐日聚合
"""
from __future__ import annotations

import logging
import math
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.services.market_overview_builder import (
    _score,
    _dimension_rank,
    _finite,
    CORE_INDEX_SYMBOLS,
)
from app.parquet import scan_enriched_parquet

logger = logging.getLogger(__name__)


def _load_index_pct_map(repo, start: date, end: date) -> dict:
    """读取主力指数日K, 计算每日各指数涨幅 → {date: {symbol: pct(百分比)}}。"""
    index_pct_map: dict = {}
    try:
        for symbol in CORE_INDEX_SYMBOLS:
            df = repo.get_index_daily(symbol, start, end, columns=["date", "change_pct"])
            if df.is_empty() or "change_pct" not in df.columns:
                continue
            for r in df.iter_rows(named=True):
                d = r["date"]
                # change_pct 在 index_daily 中是小数形式(0.025 = 2.5%), 转换为百分比
                pct = float(r.get("change_pct") or 0) * 100
                if d not in index_pct_map:
                    index_pct_map[d] = {}
                index_pct_map[d][symbol] = pct
    except Exception as e:
        logger.warning("sentiment load_index_pct_map failed: %s", e)
    return index_pct_map


def _compute_sentiment_scores(metrics: dict) -> dict:
    """计算 6 个子维度分 + 总情绪分 + 情绪标签。

    完全复用 market_overview_builder 的评分逻辑, 权重和归一化区间完全一致。

    metrics 期望字段(由 _aggregate_daily 聚合):
      avg_index_pct(百分比), up_pct, avg_pct(小数), median_pct(小数), strong_diff_pct,
      avg_vol_ratio, high_vol_pct, limit_up, seal_rate, max_boards,
      tier2_count, down_pct, strong_down_pct, mainline_avg(小数), mainline_cover_pct
    """
    # 指数维度
    index_score = _score(metrics.get("avg_index_pct", 0), -2.5, 2.5)

    # 赚钱维度 - 与看板完全一致
    profit_score = round(
        _score(metrics.get("up_pct", 50), 20, 80) * 0.45 +
        _score(metrics.get("avg_pct", 0), -0.02, 0.02) * 0.25 +
        _score(metrics.get("median_pct", 0), -0.02, 0.02) * 0.20 +
        _score(metrics.get("strong_diff_pct", 0), -8, 8) * 0.10
    )

    # 量能维度
    money_score = round(
        _score(metrics.get("avg_vol_ratio", 1), 0.6, 1.8) * 0.70 +
        _score(metrics.get("high_vol_pct", 5), 2, 12) * 0.30
    )

    # 投机维度
    speculation_score = round(
        _score(metrics.get("limit_up", 0), 5, 90) * 0.25 +
        _score(metrics.get("seal_rate", 50), 30, 85) * 0.35 +
        _score(metrics.get("max_boards", 0), 1, 8) * 0.25 +
        _score(metrics.get("tier2_count", 0), 0, 30) * 0.15
    )

    # 抗跌维度
    resilience_score = 100 - round(
        _score(metrics.get("down_pct", 50), 20, 80) * 0.55 +
        _score(metrics.get("strong_down_pct", 5), 1, 12) * 0.45
    )

    # 主线维度
    mainline_score = round(
        _score(metrics.get("mainline_avg", 0), -0.005, 0.03) * 0.65 +
        _score(metrics.get("mainline_cover_pct", 0), 1, 12) * 0.35
    ) if metrics.get("mainline_avg") is not None else 50

    # 总情绪分(6 维度简单平均)
    emotion_score = round(
        (index_score + profit_score + money_score + speculation_score + resilience_score + mainline_score) / 6
    )

    # 情绪标签 - 与看板完全一致
    if emotion_score >= 70:
        emotion_label = "强势"
    elif emotion_score >= 55:
        emotion_label = "偏暖"
    elif emotion_score >= 45:
        emotion_label = "震荡"
    elif emotion_score >= 30:
        emotion_label = "偏冷"
    else:
        emotion_label = "冰点"

    return {
        "index_score": index_score,
        "profit_score": profit_score,
        "money_score": money_score,
        "speculation_score": speculation_score,
        "resilience_score": resilience_score,
        "mainline_score": mainline_score,
        "emotion_score": emotion_score,
        "emotion_label": emotion_label,
    }


def _load_and_compute_full_enriched(repo, start: date, end: date):
    """
    性能优化版：一次性加载从 start-150 到 end 的所有数据，计算完整指标。
    与看板数据加载方式完全一致。
    """
    from app.tickflow.repository import enriched_dirname
    from app.indicators.pipeline import (
        compute_indicators,
        compute_limit_signals,
        compute_signals,
    )

    # 加载范围：start前推150天作为warmup
    load_start = start - timedelta(days=150)
    enriched_dir = repo.store.data_dir / enriched_dirname("stock")
    read_cols = ["symbol", "date", "open", "high", "low", "close", "volume",
                 "amount", "raw_close", "raw_high", "raw_low"]

    try:
        lf = (
            scan_enriched_parquet(str(enriched_dir / "**" / "*.parquet"))
            .filter(
                (pl.col("date") >= load_start)
                & (pl.col("date") <= end)
            )
            .sort(["symbol", "date"])
        )
        available = [c for c in read_cols if c in lf.schema]
        df_hist = lf.select(available).collect()
    except Exception as e:
        logger.warning("warmup history load failed: %s", e)
        return pl.DataFrame()

    if df_hist.is_empty():
        return pl.DataFrame()

    # 计算指标（与看板完全一致）
    df_full = compute_indicators(df_hist)
    df_full = compute_signals(df_full)

    # 计算涨跌停信号
    instruments = repo.get_instruments()
    if instruments is not None and not instruments.is_empty():
        df_full = compute_limit_signals(
            df_full,
            instruments,
            historical_shares=repo.get_historical_shares(),
        )

    # JOIN instruments (name等字段)
    if instruments is not None and not instruments.is_empty():
        inst_cols = [c for c in ["symbol", "name", "total_shares", "float_shares"] if c in instruments.columns]
        if "name" not in df_full.columns:
            df_full = df_full.join(instruments.select(inst_cols), on="symbol", how="left")

    # 只保留目标日期范围的数据（剔除warmup的日期）
    df_result = df_full.filter(
        (pl.col("date") >= start)
        & (pl.col("date") <= end)
    )

    return df_result


def _aggregate_single_day(repo, target_date: date, df_day: pl.DataFrame, index_pct_map: dict, depth_service=None):
    """
    与看板完全一致的单日聚合逻辑，包含五档 sealed 修正。
    """
    if df_day.is_empty():
        return None

    rows = df_day.to_dicts()

    # ===== 1. 过滤真停牌股票（与看板完全一致）=====
    if rows and "volume" in rows[0]:
        rows = [r for r in rows
                if (_finite(r.get("volume")) or 0) > 0
                or (_finite(r.get("change_pct")) or 0) != 0]

    if not rows:
        return None

    total = len(rows)

    # ===== 2. 计算各类指标（与看板完全一致）=====
    up = sum(1 for r in rows if (_finite(r.get("change_pct")) or 0) > 0)
    down = sum(1 for r in rows if (_finite(r.get("change_pct")) or 0) < 0)
    up_pct = up / total * 100 if total else 0
    down_pct = down / total * 100 if total else 0

    pct_values = [_finite(r.get("change_pct")) for r in rows]
    pct_values = [v for v in pct_values if v is not None]
    avg_pct = sum(pct_values) / len(pct_values) if pct_values else 0
    median_pct = sorted(pct_values)[len(pct_values) // 2] if pct_values else 0
    strong_up = sum(1 for v in pct_values if v >= 0.03)
    strong_down = sum(1 for v in pct_values if v <= -0.03)
    strong_diff_pct = (strong_up - strong_down) / total * 100 if total else 0
    strong_down_pct = strong_down / total * 100 if total else 0

    # ===== 3. limit_up 计算（与看板完全一致：包含连板股 + 五档 sealed 修正）=====
    limit_up = sum(
        1 for r in rows
        if bool(r.get("signal_limit_up")) or (_finite(r.get("consecutive_limit_ups")) or 0) > 0
    )
    broken = sum(1 for r in rows if bool(r.get("signal_broken_limit_up")))
    max_boards = max([int(_finite(r.get("consecutive_limit_ups")) or 0) for r in rows], default=0)

    # 五档 sealed 修正（与看板完全一致）: 未就绪时计数为 0, 不修正
    fake_up = depth_service.fake_limit_count(target_date, is_down=False) if depth_service else 0
    if fake_up:
        limit_up = max(0, limit_up - fake_up)

    # 计算 tier2_count（与看板完全一致：tiers_map 方式）
    tiers_map = {}
    for r in rows:
        n = int(_finite(r.get("consecutive_limit_ups")) or 0)
        if n > 0:
            tiers_map[n] = tiers_map.get(n, 0) + 1
    tier2_count = sum(v for k, v in tiers_map.items() if k >= 2)

    seal_rate = limit_up / (limit_up + broken) * 100 if (limit_up + broken) > 0 else 0

    # ===== 4. 量能指标 =====
    vol_ratios = [_finite(r.get("vol_ratio_5d")) for r in rows]
    vol_ratios = [v for v in vol_ratios if v is not None]
    avg_vol_ratio = sum(vol_ratios) / len(vol_ratios) if vol_ratios else 1
    high_vol_ratio = sum(1 for v in vol_ratios if v >= 1.5)
    high_vol_pct = high_vol_ratio / total * 100 if total else 0

    # ===== 5. 指数涨幅 =====
    index_pct_for_date = index_pct_map.get(target_date, {})
    avg_index_pct = (
        sum(v for v in index_pct_for_date.values() if v is not None) / len(index_pct_for_date)
        if index_pct_for_date
        else 0
    )

    # ===== 6. 主线维度 =====
    mainline_avg = 0.0
    mainline_cover_pct = 0.0
    concept_rank = _dimension_rank(rows, repo, "concept")
    industry_rank = _dimension_rank(rows, repo, "industry", level=2)
    mainline_items = [*concept_rank["leading"][:3], *industry_rank["leading"][:3]]
    if mainline_items:
        mainline_avg = max([_finite(item.get("avg_pct")) or 0 for item in mainline_items], default=0)
        mainline_cover_pct = max([(_finite(item.get("count")) or 0) / total * 100 for item in mainline_items], default=0) if total else 0

    # ===== 7. 组装 metrics 并计算评分 =====
    metrics = {
        "date": target_date,
        "avg_index_pct": avg_index_pct,
        "up_pct": up_pct,
        "avg_pct": avg_pct,
        "median_pct": median_pct,
        "strong_diff_pct": strong_diff_pct,
        "avg_vol_ratio": avg_vol_ratio,
        "high_vol_pct": high_vol_pct,
        "limit_up": limit_up,
        "seal_rate": seal_rate,
        "max_boards": max_boards,
        "tier2_count": tier2_count,
        "down_pct": down_pct,
        "strong_down_pct": strong_down_pct,
        "mainline_avg": mainline_avg,
        "mainline_cover_pct": mainline_cover_pct,
    }

    scores = _compute_sentiment_scores(metrics)

    return {
        "date": target_date,
        # 原始指标
        "avg_index_pct": round(avg_index_pct, 4),
        "up_pct": round(up_pct, 4),
        "avg_pct": round(avg_pct, 4),
        "median_pct": round(median_pct, 4),
        "strong_diff_pct": round(strong_diff_pct, 4),
        "avg_vol_ratio": round(avg_vol_ratio, 4),
        "high_vol_pct": round(high_vol_pct, 4),
        "limit_up": limit_up,
        "seal_rate": round(seal_rate, 4),
        "max_boards": max_boards,
        "tier2_count": tier2_count,
        "down_pct": round(down_pct, 4),
        "strong_down_pct": round(strong_down_pct, 4),
        "mainline_avg": round(mainline_avg, 4),
        "mainline_cover_pct": round(mainline_cover_pct, 4),
        # 计算结果
        "index_score": scores["index_score"],
        "profit_score": scores["profit_score"],
        "money_score": scores["money_score"],
        "speculation_score": scores["speculation_score"],
        "resilience_score": scores["resilience_score"],
        "mainline_score": scores["mainline_score"],
        "emotion_score": scores["emotion_score"],
        "emotion_label": scores["emotion_label"],
    }


def run_sentiment_batch(repo, start: date, end: date, depth_service=None) -> pl.DataFrame:
    """批算 [start, end] 的情绪周期时序。性能优化版：一次性加载 + 计算。"""
    if start > end:
        logger.warning("run_sentiment_batch: start > end (%s > %s)", start, end)
        return pl.DataFrame()

    logger.info("run_sentiment_batch: computing sentiment from %s to %s", start, end)

    # 指数涨幅(所有主力指数)
    index_pct_map = _load_index_pct_map(repo, start, end)
    logger.info("run_sentiment_batch: loaded %d index_pct entries", len(index_pct_map))

    # 一次性加载完整日期范围 + warmup，计算所有指标
    df_full = _load_and_compute_full_enriched(repo, start, end)
    if df_full.is_empty():
        logger.warning("sentiment batch: no enriched data for [%s~%s]", start, end)
        return pl.DataFrame()

    logger.info("run_sentiment_batch: loaded & computed full enriched, %d rows", df_full.height)

    # 获取所有唯一日期
    unique_dates = df_full.select("date").unique().to_series().sort().to_list()
    logger.info("run_sentiment_batch: %d unique dates to aggregate", len(unique_dates))

    # 逐日聚合
    rows = []
    for target_date in unique_dates:
        df_day = df_full.filter(pl.col("date") == target_date)
        row = _aggregate_single_day(repo, target_date, df_day, index_pct_map, depth_service)
        if row is not None:
            rows.append(row)

    result = pl.DataFrame(rows) if rows else pl.DataFrame()
    logger.info("run_sentiment_batch: aggregated %d rows", result.height if not result.is_empty() else 0)

    return result


# ───────────────────────── 持久化(upsert)（保持原样）─────────────────────────

SENTIMENT_DIR = "sentiment_history"


def sentiment_path(data_dir: Path) -> Path:
    return data_dir / SENTIMENT_DIR / "part.parquet"


def load_sentiment_history(data_dir: Path) -> pl.DataFrame:
    """读取全部 sentiment 时序; 不存在返回空 DataFrame。"""
    p = sentiment_path(data_dir)
    if not p.exists():
        return pl.DataFrame()
    try:
        return pl.read_parquet(p)
    except Exception as e:
        logger.warning("load_sentiment_history failed: %s", e)
        return pl.DataFrame()


def upsert_sentiment_history(data_dir: Path, new_rows: pl.DataFrame) -> None:
    """按 date 覆盖(upsert): 重算的天覆盖旧行, 新天追加。"""
    if new_rows.is_empty() or "date" not in new_rows.columns:
        return
    p = sentiment_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    new_dates = set(new_rows["date"].to_list())
    old = load_sentiment_history(data_dir)
    if old.is_empty():
        combined = new_rows
    else:
        kept = old.filter(~pl.col("date").is_in(list(new_dates)))
        # schema 对齐: 以 new_rows 的列名+顺序为权威
        target_cols = new_rows.columns
        keep_exprs = []
        for c in target_cols:
            if c in kept.columns:
                keep_exprs.append(pl.col(c))
            else:
                keep_exprs.append(pl.lit(None).alias(c))
        kept = kept.select(keep_exprs)
        new_rows = new_rows.select(target_cols)
        combined = pl.concat([kept, new_rows], how="vertical_relaxed")
    combined = combined.sort("date").unique(subset=["date"], keep="last")
    combined.write_parquet(p)


def get_sentiment_coverage(data_dir: Path) -> dict:
    """返回 sentiment 时序的覆盖元信息(供数据画像/API)。"""
    df = load_sentiment_history(data_dir)
    if df.is_empty():
        return {"rows": 0, "earliest_date": None, "latest_date": None}
    return {
        "rows": df.height,
        "earliest_date": str(df["date"].min()),
        "latest_date": str(df["date"].max()),
    }


def detect_stale_dates(data_dir: Path, repo) -> list[date]:
    """检测 sentiment 已有但需要重算的天(enriched 被覆写)。"""
    sentiment_p = sentiment_path(data_dir)
    if not sentiment_p.exists():
        return []
    sentiment_mtime = sentiment_p.stat().st_mtime
    enriched_dir = repo.store.data_dir / "kline_daily_enriched"
    if not enriched_dir.exists():
        return []
    stale: list[date] = []
    existing = load_sentiment_history(data_dir)
    if existing.is_empty():
        return []
    existing_dates = set(existing["date"].to_list())
    for part in enriched_dir.glob("date=*/part.parquet"):
        try:
            ds = part.parent.name.replace("date=", "")
            d = date.fromisoformat(ds)
        except (ValueError, OSError):
            continue
        if d not in existing_dates:
            continue
        try:
            if part.stat().st_mtime > sentiment_mtime:
                stale.append(d)
        except OSError:
            continue
    return sorted(stale)


def compute_sentiment_incremental(repo, data_dir: Path, *, today: date | None = None, depth_service=None) -> pl.DataFrame:
    """增量计算 sentiment(供 daily_pipeline / 启动补算调用)。"""
    today = today or date.today()
    existing = load_sentiment_history(data_dir)

    # 缺口: enriched 有哪些天, sentiment 缺哪些
    enriched_dates = set()
    enriched_dir = repo.store.data_dir / "kline_daily_enriched"
    if enriched_dir.exists():
        for part in enriched_dir.glob("date=*/part.parquet"):
            try:
                ds = part.parent.name.replace("date=", "")
                enriched_dates.add(date.fromisoformat(ds))
            except (ValueError, OSError):
                continue

    existing_dates = set(existing["date"].to_list()) if not existing.is_empty() else set()
    missing = sorted(d for d in enriched_dates if d not in existing_dates and d <= today)

    # stale: enriched 覆写过
    stale = detect_stale_dates(data_dir, repo)

    to_compute = sorted(set(missing) | set(stale))
    if not to_compute:
        logger.debug("sentiment incremental: nothing to compute")
        return pl.DataFrame()

    logger.info("sentiment incremental: compute %d days (missing=%d, stale=%d)",
                len(to_compute), len(missing), len(stale))
    new_rows = run_sentiment_batch(repo, start=to_compute[0], end=to_compute[-1], depth_service=depth_service)
    if not new_rows.is_empty():
        upsert_sentiment_history(data_dir, new_rows)
    return new_rows


def earliest_enriched_date(repo) -> date | None:
    """返回 enriched 最早日期(供全量重算定起点)。无数据返回 None。"""
    enriched_dir = repo.store.data_dir / "kline_daily_enriched"
    dates: set[date] = set()
    if not enriched_dir.exists():
        logger.warning("earliest_enriched_date: enriched_dir does not exist: %s", enriched_dir)
        return None

    for part in enriched_dir.glob("date=*/part.parquet"):
        try:
            ds = part.parent.name.replace("date=", "")
            dates.add(date.fromisoformat(ds))
        except ValueError:
            continue

    if dates:
        return min(dates)
    return None
