"""股价查询对外 API (vpush 调用) — Bearer token 鉴权, 与面板会话认证分离。

端点:
  GET  /api/v1/price?code=sh600519&at=...   — 单笔时刻价查询
  POST /api/v1/prices                       — 批量查询 (≤50 item, 逐 item 独立成败)
  POST /api/v1/token                        — (面板登录态) 生成/轮换 Bearer token
  DELETE /api/v1/token                      — (面板登录态) 吊销 token

鉴权 (docs/price-query-api.md §5):
  /api/v1/price* 走 Authorization: Bearer <token>, 由认证中间件白名单放行
  面板会话检查后在此自行校验; token 存 secrets_store (vpush_api_token),
  未配置时一律 401 (fail-closed)。token 管理端点走面板会话 (不在白名单内,
  中间件正常拦截), 复用站内权限体系。
"""
from __future__ import annotations

import secrets as py_secrets
from datetime import datetime

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app import secrets_store
from app.services.price_query import (
    PriceQueryService,
    _parse_at,
)

router = APIRouter(prefix="/api/v1", tags=["price-query"])

MAX_ITEMS = 50
_TOKEN_FIELD = "vpush_api_token"


def _check_bearer(authorization: str | None) -> None:
    """校验 Bearer token; 未配置或不匹配一律 401 (fail-closed)。"""
    expected = secrets_store.load().get(_TOKEN_FIELD) or ""
    if not expected:
        raise HTTPException(status_code=401, detail="服务端未配置访问 token")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer token")
    token = authorization[len("Bearer "):].strip()
    # 常数时间比较, 防时序侧信道
    if not py_secrets.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="token 无效")


def _parse_code_or_400(code: str) -> str:
    """格式校验 (位数/数字); 前缀支持范围 (bj) 交服务层判 unknown_code。"""
    raw = (code or "").strip().lower()
    if len(raw) == 8 and raw[:2].isalpha() and raw[2:].isdigit():
        return raw
    raise HTTPException(status_code=400, detail=f"code 非法: {code}")


# ================================================================
# 查询端点 (Bearer 鉴权)
# ================================================================

@router.get("/price")
def get_price(
    request: Request,
    code: str,
    at: str | None = None,
    authorization: str | None = Header(default=None),
) -> dict:
    """单笔时刻价查询。at 省略 = 查最新价。"""
    _check_bearer(authorization)
    code = _parse_code_or_400(code)
    at_dt: datetime | None = None
    if at is not None:
        at_dt = _parse_at(at)
        if at_dt is None:
            raise HTTPException(status_code=400, detail="at 格式非法, 应为 YYYY-MM-DDTHH:MM:SS")

    service = PriceQueryService(request.app.state.repo)
    res = service.query_batch([(code, at_dt)])[0]
    if res.error == "unknown_code":
        # 含前缀不支持 (bj 无北交所数据, docs/price-query-api.md §6)
        raise HTTPException(status_code=404, detail=f"代码不存在或不支持: {code}")
    if res.error == "no_data":
        raise HTTPException(status_code=404, detail=f"无可用价格数据: {code}")
    if res.error:
        raise HTTPException(status_code=500, detail=res.error)
    return {
        "code": res.code,
        "name": res.name,
        "at": res.at.isoformat() if res.at else None,
        "price": res.price,
        "actual_at": res.actual_at.isoformat() if res.actual_at else None,
        "kind": res.kind,
    }


class PriceItemIn(BaseModel):
    code: str = Field(min_length=2, max_length=16)
    at: str | None = None


class PricesIn(BaseModel):
    items: list[PriceItemIn]


@router.post("/prices")
def get_prices(req: PricesIn, request: Request, authorization: str | None = Header(default=None)) -> dict:
    """批量查询。整体 400 仅限结构错误 (空/超限/code 或 at 非法); 单项数据
    缺失在 item 内表达 (status: error), 不拖垮整单。"""
    _check_bearer(authorization)
    if not req.items or len(req.items) > MAX_ITEMS:
        raise HTTPException(status_code=400, detail=f"items 数量非法 (1~{MAX_ITEMS})")

    items: list[tuple[str, datetime | None]] = []
    for item in req.items:
        # 格式校验 (位数/数字); 前缀支持范围 (bj) 交服务层按 item 报 unknown_code
        code = _parse_code_or_400(item.code)
        at_dt: datetime | None = None
        if item.at is not None:
            at_dt = _parse_at(item.at)
            if at_dt is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"at 格式非法, 应为 YYYY-MM-DDTHH:MM:SS: {item.at}",
                )
        items.append((code, at_dt))

    service = PriceQueryService(request.app.state.repo)
    results = service.query_batch(items)

    out_items: list[dict] = []
    for item, res in zip(req.items, results, strict=True):
        if res.error:
            entry: dict = {"code": item.code, "status": "error", "error": res.error}
        else:
            entry = {
                "code": item.code,
                "status": "ok",
                "name": res.name,
                "price": res.price,
                "actual_at": res.actual_at.isoformat() if res.actual_at else None,
                "kind": res.kind,
            }
        if item.at is not None:
            entry["at"] = item.at
        out_items.append(entry)
    return {"items": out_items}


# ================================================================
# token 管理 (面板会话鉴权 — 不在中间件白名单内)
# ================================================================

@router.post("/token")
def create_token(request: Request) -> dict:
    """生成/轮换 vpush 调用 token (需面板登录态)。

    再次调用即轮换: 旧 token 立即失效 (secrets.json 覆盖写)。
    """
    _require_panel_session(request)
    token = py_secrets.token_urlsafe(32)
    secrets_store.save({_TOKEN_FIELD: token})
    return {"token": token}


@router.delete("/token")
def revoke_token(request: Request) -> dict:
    """吊销 vpush 调用 token (需面板登录态)。吊销后查询端点对所有人 401。"""
    _require_panel_session(request)
    secrets_store.clear(_TOKEN_FIELD)
    return {"ok": True}


def _require_panel_session(request: Request) -> None:
    """面板会话校验 (与 auth 中间件同口径, 防白名单误配绕过)。"""
    from app.api.auth import COOKIE_NAME
    from app.services import auth as auth_service

    token = request.cookies.get(COOKIE_NAME)
    if not (token and auth_service.is_valid_session(token)):
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
