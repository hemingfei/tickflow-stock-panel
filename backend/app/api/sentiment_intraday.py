"""实时情绪 API - 分钟级情绪指标。"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Query, Request

from app.services.intraday_sentiment import (
    get_intraday_sentiment_service,
    is_trading_time,
    load_intraday_sentiment,
)

router = APIRouter(prefix="/api/sentiment/intraday", tags=["sentiment-intraday"])
logger = logging.getLogger(__name__)


@router.get("/latest")
def intraday_sentiment_latest(request: Request, target_date: date | None = Query(None)):
    """获取最新的实时情绪数据。"""
    service = get_intraday_sentiment_service()
    service.set_repo(request.app.state.repo)
    service.set_depth_service(getattr(request.app.state, "depth_service", None))
    latest = service.get_latest(target_date)
    return {"data": latest}


@router.get("/history")
def intraday_sentiment_history(request: Request, target_date: date | None = Query(None)):
    """获取当日的历史分钟级情绪数据。"""
    service = get_intraday_sentiment_service()
    service.set_repo(request.app.state.repo)
    history = service.get_history(target_date)
    return {"data": history, "count": len(history)}


@router.get("/status")
def intraday_sentiment_status(request: Request):
    """获取实时情绪服务状态。"""
    now = datetime.now()
    trading = is_trading_time(now)
    return {
        "trading_time": trading,
        "current_time": now.isoformat(),
    }


@router.post("/compute")
def intraday_sentiment_compute(request: Request, force: bool = Query(False)):
    """手动触发一次实时情绪计算。"""
    service = get_intraday_sentiment_service()
    service.set_repo(request.app.state.repo)
    service.set_depth_service(getattr(request.app.state, "depth_service", None))
    result = service.compute_now(force=force)
    return {"success": result is not None, "data": result}
