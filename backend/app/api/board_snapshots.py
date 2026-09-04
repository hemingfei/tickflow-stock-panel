"""看板快照回溯 API — 枚举已落盘的看板快照并按日期+时刻取最近节点。

提供两组路由:
  - /api/board-snapshots/*  站内「看板回溯」页使用, 走登录认证;
  - /api/public/replay/*    免登录独立回放页使用 (认证中间件白名单),
                            响应剥离监控中心告警 — 告警含用户策略名与
                            自选标的, 不对外泄露; 行情数据保持完整。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.services import board_snapshot_store

router = APIRouter(prefix="/api/board-snapshots", tags=["overview"])
public_router = APIRouter(prefix="/api/public/replay", tags=["overview"])


def _strip_private(snapshot: dict) -> dict:
    """公开响应剥离个人信息块 (只替换 alerts 键, 不改动快照其余内容)。"""
    public = dict(snapshot)
    public["alerts"] = {"alerts": [], "total": 0}
    return public


def _dates_payload(request: Request) -> dict:
    data_dir = request.app.state.repo.store.data_dir
    return {"dates": board_snapshot_store.list_dates(data_dir)}


def _times_payload(request: Request, date: str) -> dict:
    data_dir = request.app.state.repo.store.data_dir
    try:
        times = board_snapshot_store.list_times(data_dir, date)
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式非法, 应为 YYYY-MM-DD") from None
    return {"date": board_snapshot_store.normalize_day(date), "times": times}


def _load_payload(request: Request, date: str | None, time: str | None, *, public: bool) -> dict:
    """取指定日期时刻最近的看板快照 (不晚于该时刻的最近节点)。

    date 缺省取最新有快照的日期; time 缺省取该日最后一个节点。
    URL 带日期时间直达时由前端映射到最近节点后调用。
    """
    data_dir = request.app.state.repo.store.data_dir
    try:
        day = board_snapshot_store.normalize_day(date) if date else None
        if time is not None:
            time = board_snapshot_store.normalize_hhmm(time)
    except ValueError:
        raise HTTPException(status_code=400, detail="日期/时间格式非法, 应为 YYYY-MM-DD 与 HH:MM") from None

    if day is None:
        dates = board_snapshot_store.list_dates(data_dir)
        if not dates:
            raise HTTPException(status_code=404, detail="暂无看板快照")
        day = dates[-1]

    result = board_snapshot_store.load_nearest(data_dir, day, time)
    if result is None:
        raise HTTPException(status_code=404, detail=f"{day} 暂无看板快照")
    snapshot_time, snapshot = result
    return {
        "requested_date": day,
        "requested_time": time,
        "snapshot_time": snapshot_time,
        "snapshot": _strip_private(snapshot) if public else snapshot,
    }


# ===== 站内 (需登录) =====

@router.get("/dates")
def snapshot_dates(request: Request):
    """有看板快照的日期列表 (升序), 供回溯页日期选择。"""
    return _dates_payload(request)


@router.get("/times")
def snapshot_times(request: Request, date: str):
    """指定日期的快照时刻列表 ("HH:MM" 升序), 供时间滑条定位节点。"""
    return _times_payload(request, date)


@router.get("/load")
def snapshot_load(request: Request, date: str | None = None, time: str | None = None):
    return _load_payload(request, date, time, public=False)


# ===== 公开回放 (免登录, 独立 URL) =====

@public_router.get("/dates")
def public_snapshot_dates(request: Request):
    return _dates_payload(request)


@public_router.get("/times")
def public_snapshot_times(request: Request, date: str):
    return _times_payload(request, date)


@public_router.get("/load")
def public_snapshot_load(request: Request, date: str | None = None, time: str | None = None):
    return _load_payload(request, date, time, public=True)
