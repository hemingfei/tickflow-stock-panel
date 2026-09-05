"""实时环境 API - 分钟级环境综合分及四维度数据。"""
from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Query, Request

from app.services.intraday_regime import (
    INTRADAY_REGIME_DIR,
    get_intraday_regime_service,
    is_trading_time,
    load_intraday_regime,
)

router = APIRouter(prefix="/api/regime/intraday", tags=["regime-intraday"])
logger = logging.getLogger(__name__)


@router.get("/latest")
def intraday_regime_latest(request: Request, target_date: date | None = Query(None)):
    """获取最新的实时环境数据。"""
    service = get_intraday_regime_service()
    service.set_repo(request.app.state.repo)
    latest = service.get_latest(target_date)
    return {"data": latest}


@router.get("/history")
def intraday_regime_history(request: Request, target_date: date | None = Query(None)):
    """获取当日的历史分钟级环境数据。"""
    service = get_intraday_regime_service()
    service.set_repo(request.app.state.repo)
    history = service.get_history(target_date)
    return {"data": history, "count": len(history)}


@router.get("/status")
def intraday_regime_status(request: Request):
    """获取实时环境服务状态。"""
    now = datetime.now()
    trading = is_trading_time(now)
    return {
        "trading_time": trading,
        "current_time": now.isoformat(),
    }


@router.post("/compute")
def intraday_regime_compute(request: Request, force: bool = Query(False)):
    """手动触发一次实时环境计算。"""
    service = get_intraday_regime_service()
    service.set_repo(request.app.state.repo)
    result = service.compute_now(force=force)
    return {"success": result is not None, "data": result}


@router.get("/dates")
def intraday_regime_dates(request: Request):
    """获取可用的日期列表。"""
    repo = request.app.state.repo
    data_dir = Path(repo.store.data_dir) / INTRADAY_REGIME_DIR
    
    if not data_dir.exists():
        return {"dates": []}
    
    dates = []
    
    # 扫描 .parquet 文件
    for file in data_dir.glob("*.parquet"):
        date_str = file.stem
        try:
            # 验证日期格式
            parsed_date = date.fromisoformat(date_str)
            dates.append(parsed_date)
        except ValueError:
            pass
    
    # 去重并按日期降序排序
    unique_dates = sorted(list(set(dates)), reverse=True)
    
    return {"dates": [d.isoformat() for d in unique_dates]}


# ===== 公开实时环境 (免登录独立页, 认证中间件白名单) =====
# 只读端点复用站内同一实现; 数据为全市场聚合口径, 无个人策略/自选信息;
# compute (触发服务端计算) 不公开。
public_router = APIRouter(prefix="/api/public/env/regime", tags=["regime-intraday"])


@public_router.get("/history")
def public_intraday_regime_history(request: Request, target_date: date | None = None):
    return intraday_regime_history(request, target_date)


@public_router.get("/status")
def public_intraday_regime_status(request: Request):
    return intraday_regime_status(request)


@public_router.get("/dates")
def public_intraday_regime_dates(request: Request):
    return intraday_regime_dates(request)

