"""情绪周期(sentiment) API — 时序查询 + 手动重算。

装配逻辑在 app.services.sentiment_builder(纯函数), API 层薄壳 + TTL 缓存。
完全复用 regime API 的设计模式。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date
from typing import Any

from fastapi import APIRouter, Query, Request

from app.services import sentiment_builder

router = APIRouter(prefix="/api/sentiment", tags=["sentiment"])

_CACHE_TTL = 5.0
_cache: dict[str, Any] | None = None
_cache_ts: float = 0.0
_cache_lock = threading.Lock()

logger = logging.getLogger(__name__)


def invalidate_sentiment_cache() -> None:
    """清空 sentiment 查询缓存。批算/重算后调用。"""
    global _cache, _cache_ts
    with _cache_lock:
        _cache = None
        _cache_ts = 0.0


def _data_dir(request: Request) -> Any:
    return request.app.state.repo.store.data_dir


def _df_to_records(df) -> list[dict]:
    """polars DataFrame → JSON 安全的 list[dict](date 转 ISO 字符串)。"""
    if df is None or df.is_empty():
        return []
    records = []
    for r in df.to_dicts():
        if "date" in r and r["date"] is not None:
            r["date"] = str(r["date"])
        records.append(r)
    return records


@router.get("/history")
def sentiment_history(
    request: Request,
    start: date | None = Query(None),
    end: date | None = Query(None),
    limit: int = Query(120, ge=1, le=1000),
):
    """历史情绪周期时序(含 6 维度分 + 总情绪分 + 标签)。默认最近 N 天。"""
    global _cache, _cache_ts
    cache_key = f"hist|{start}|{end}|{limit}"
    with _cache_lock:
        if (
            _cache is not None
            and _cache.get("key") == cache_key
            and (time.time() - _cache_ts) < _CACHE_TTL
        ):
            return _cache["data"]

    df = sentiment_builder.load_sentiment_history(_data_dir(request))
    if df.is_empty():
        result: dict = {"rows": [], "total": 0}
    else:
        if start:
            df = df.filter(pl_col_date(df, ">=", start))
        if end:
            df = df.filter(pl_col_date(df, "<=", end))
        # limit 仅在"最近 N 天"模式(未传 start/end)生效;
        # 日期范围模式(传了 start/end, 如"全部")应返回完整范围, 不截断。
        if start is None and end is None:
            df = df.sort("date", descending=True).head(limit)
        df = df.sort("date")
        rows = _df_to_records(df)
        result = {"rows": rows, "total": len(rows)}

    with _cache_lock:
        _cache = {"key": cache_key, "data": result}
        _cache_ts = time.time()
    return result


def pl_col_date(df, op: str, value: date):
    """polars 日期过滤辅助(避免重复 import)。"""
    import polars as pl

    col = pl.col("date")
    return col >= value if op == ">=" else col <= value


@router.get("/latest")
def sentiment_latest(request: Request):
    """最新一日情绪周期(轻量)。"""
    df = sentiment_builder.load_sentiment_history(_data_dir(request))
    if df.is_empty():
        return {"row": None}
    latest = df.sort("date", descending=True).head(1)
    rows = _df_to_records(latest)
    return {"row": rows[0] if rows else None}


@router.get("/coverage")
def sentiment_coverage(request: Request):
    """sentiment 数据覆盖元信息(供数据画像)。"""
    return sentiment_builder.get_sentiment_coverage(_data_dir(request))


@router.post("/recompute")
def sentiment_recompute(request: Request, start: date | None = None, end: date | None = None):
    """手动触发重算(全量或指定区间)。管理员操作。

    - 不传 start: 强制全量重算(enriched 最早日 ~ 今天), 覆盖所有已有行。
      与 daily_pipeline 的增量补差(compute_sentiment_incremental)不同 —— 此接口面向
      人工「我要重新算一遍」的预期, 必须真正重算而非增量补缺口。
    - 传 start: 仅重算 [start, end] 区间。
    """
    repo = request.app.state.repo
    data_dir = _data_dir(request)
    end = end or date.today()
    
    logger.info("sentiment_recompute called with start=%s, end=%s", start, end)
    
    if start is None:
        # 全量: 从 enriched 最早日强制重算到今天
        earliest = sentiment_builder.earliest_enriched_date(repo)
        logger.info("sentiment_recompute: earliest_enriched_date = %s", earliest)
        if earliest is None:
            invalidate_sentiment_cache()
            return {"ok": True, "computed": 0}
        start = earliest
    
    logger.info("sentiment_recompute: computing from %s to %s", start, end)
    new_rows = sentiment_builder.run_sentiment_batch(repo, start=start, end=end)
    logger.info("sentiment_recompute: new_rows.height = %s", new_rows.height if not new_rows.is_empty() else 0)
    
    if not new_rows.is_empty():
        sentiment_builder.upsert_sentiment_history(data_dir, new_rows)
    invalidate_sentiment_cache()
    return {"ok": True, "computed": new_rows.height if not new_rows.is_empty() else 0}


@router.post("/refresh")
def sentiment_refresh(request: Request):
    """增量刷新:只计算当天、缺失的、以及stale的数据(快)。"""
    repo = request.app.state.repo
    data_dir = _data_dir(request)
    logger.info("sentiment_refresh called")
    new_rows = sentiment_builder.compute_sentiment_incremental(repo, data_dir)
    logger.info("sentiment_refresh: new_rows.height = %s", new_rows.height if not new_rows.is_empty() else 0)
    invalidate_sentiment_cache()
    return {"ok": True, "computed": new_rows.height if not new_rows.is_empty() else 0}
