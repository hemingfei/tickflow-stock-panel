"""指数共振 API — 监测配置 CRUD 与实时共振状态。

状态接口 GET /api/resonance/state 返回所有监测的配置 + 最新计算状态。
所有涨跌幅/窗口涨幅字段统一为百分数口径 (3.66 = 3.66%), 前端直接展示。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from app.services.index_const import CORE_INDEX_SYMBOLS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/resonance", tags=["resonance"])


class TimeRangeItem(BaseModel):
    start: str = Field(..., description="开始时间 HH:MM (北京时间)")
    end: str = Field(..., description="结束时间 HH:MM (北京时间)")


class MonitorUpsertRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=50, description="监测名称")
    index_symbol: str = Field(..., description="核心指数代码, 如 399006.SZ")
    enabled: bool = Field(default=True)
    time_ranges: list[TimeRangeItem] = Field(
        default_factory=list,
        description="生效时间段列表; 空 = 整个连续竞价时段",
    )
    window_seconds: int = Field(default=300, ge=30, le=3600, description="动量窗口(秒), 30-3600")
    index_threshold_pct: float = Field(
        default=0.3, gt=0, le=10, description="指数窗口涨幅阈值(百分数, 0.3 = 0.3%)",
    )
    group_up_ratio: float = Field(
        default=0.6, ge=0, le=1, description="板块效应门槛: 组内上涨家数占比",
    )
    min_group_members: int = Field(default=3, ge=2, le=500, description="分组有效成员数下限")
    group_ids: list[str] = Field(default_factory=list, description="参与比较的分组; 空 = 全部分组")
    # 三维判定附加阈值: 涨幅门禁 <0 停用; 量能倍数 <=0 停用。
    # 量比 = 窗口每分钟成交量 / 窗口起点时刻的当日每分钟平均量。
    index_change_pct_gate: float = Field(
        default=0.2, ge=-1, le=20, description="指数当前涨幅门禁(百分数); <0 停用",
    )
    index_volume_ratio_gate: float = Field(
        default=1.5, ge=0, le=20, description="指数量比门禁(倍); <=0 停用",
    )
    group_change_pct_gate: float = Field(
        default=0.0, ge=-1, le=20, description="分组当前平均涨幅门禁(百分数); <0 停用",
    )
    group_volume_ratio_gate: float = Field(
        default=1.3, ge=0, le=20, description="分组量比门禁(倍); <=0 停用",
    )
    leader_change_pct_gate: float = Field(
        default=0.5, ge=-1, le=20, description="龙头当前涨幅门禁(百分数); <0 停用",
    )
    leader_volume_ratio_gate: float = Field(
        default=1.5, ge=0, le=20, description="龙头量比门禁(倍); <=0 停用",
    )
    webhook_channels: list[str] = Field(
        default_factory=list,
        description="推送渠道 (feishu/wecom/kol/custom/email); 空 = 不推 webhook",
    )
    notify_cooldown_seconds: int = Field(
        default=600, ge=0, le=86400,
        description="共振通知冷却(秒); 0 = 仅按上升沿去重",
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("监测名称不能为空")
        return v

    @field_validator("index_symbol")
    @classmethod
    def validate_index_symbol(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in CORE_INDEX_SYMBOLS:
            raise ValueError(f"指数代码必须是核心四只之一: {', '.join(CORE_INDEX_SYMBOLS)}")
        return v


def _service(request: Request):
    service = getattr(request.app.state, "resonance_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="指数共振服务未初始化")
    return service


@router.get("/monitors")
def list_monitors(request: Request):
    """全部监测配置。"""
    return {"monitors": _service(request).list_monitors()}


@router.post("/monitors")
def create_monitor(request: Request, body: MonitorUpsertRequest):
    try:
        return {"monitors": _service(request).create_monitor(body.model_dump())}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


@router.put("/monitors/{monitor_id}")
def update_monitor(request: Request, monitor_id: str, body: MonitorUpsertRequest):
    try:
        return {"monitors": _service(request).update_monitor(monitor_id, body.model_dump())}
    except KeyError:
        raise HTTPException(status_code=404, detail="监测不存在") from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


@router.delete("/monitors/{monitor_id}")
def delete_monitor(request: Request, monitor_id: str):
    try:
        return {"monitors": _service(request).delete_monitor(monitor_id)}
    except KeyError:
        raise HTTPException(status_code=404, detail="监测不存在") from None


@router.get("/state")
def resonance_state(request: Request):
    """全部监测的配置 + 最新实时状态 (含停用/时段外的静态状态)。"""
    return _service(request).get_state()
