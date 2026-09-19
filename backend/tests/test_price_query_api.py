"""股价查询对外 API (/api/v1/price*) 测试: Bearer 鉴权、参数校验、批量行为、
token 管理 + 认证中间件白名单放行。"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import price_query as price_api
from app.services import price_query as pq

SYMBOL = "600519.SH"
CODE = "sh600519"
D1, D2 = date(2026, 8, 31), date(2026, 9, 1)
TOKEN = "test-token-abc123"


def _write_daily(data_dir: Path, day: date, close: float) -> None:
    part = data_dir / "kline_daily" / f"date={day.isoformat()}"
    part.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": [SYMBOL],
        "date": [day],
        "open": [1448.0],
        "close": [close],
        "volume": [28000.0],
        "amount": [28000000.0],
        "quote_ts": [0],
    }).write_parquet(part / "part.parquet")


@pytest.fixture()
def repo(tmp_path: Path):
    _write_daily(tmp_path, D1, close=1450.0)   # 前日收盘 1450
    _write_daily(tmp_path, D2, close=1455.0)   # 当日收盘 1455
    return SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        # 维表含 300750.SZ (真实存在) 但无本地日K → no_data 而非 unknown_code
        get_name_map=lambda symbols=None: {
            SYMBOL: "贵州茅台", "300750.SZ": "宁德时代",
        },
        get_enriched_latest=lambda: (pl.DataFrame(), None),
    )


@pytest.fixture()
def client(repo, monkeypatch):
    """独立 app (无主应用中间件), secrets_store 指向临时文件。"""
    monkeypatch.setattr(
        "app.secrets_store.load", lambda: {price_api._TOKEN_FIELD: TOKEN}
    )
    app = FastAPI()
    app.state.repo = repo
    app.include_router(price_api.router)
    return TestClient(app)


def _auth(token: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ================================================================
# 鉴权
# ================================================================

def test_missing_bearer_401(client):
    r = client.get("/api/v1/price", params={"code": CODE})
    assert r.status_code == 401


def test_wrong_bearer_401(client):
    r = client.get("/api/v1/price", params={"code": CODE}, headers=_auth("wrong"))
    assert r.status_code == 401


def test_no_token_configured_401(client, monkeypatch):
    monkeypatch.setattr("app.secrets_store.load", lambda: {})
    r = client.get("/api/v1/price", params={"code": CODE}, headers=_auth("anything"))
    assert r.status_code == 401


# ================================================================
# 单笔查询
# ================================================================

def test_get_price_at(client):
    r = client.get(
        "/api/v1/price",
        params={"code": CODE, "at": "2026-09-01T09:16:00"},
        headers=_auth(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == CODE
    assert body["name"] == "贵州茅台"
    assert body["at"] == "2026-09-01T09:16:00"
    assert body["price"] == 1450.0       # 09:16 集合竞价期 → 前日收盘
    assert body["actual_at"] == "2026-08-31T15:00:00"
    assert body["kind"] == "close"


def test_get_price_latest(client):
    r = client.get("/api/v1/price", params={"code": CODE}, headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["at"] is None
    assert body["price"] == 1455.0
    assert body["kind"] == "close"


def test_get_price_bad_code_400(client):
    r = client.get("/api/v1/price", params={"code": "xx12345"}, headers=_auth())
    assert r.status_code == 400


def test_get_price_bj_prefix_404(client):
    """bj 前缀: 服务层判 unknown_code (无北交所数据), 单笔端点 404。"""
    r = client.get("/api/v1/price", params={"code": "bj430047"}, headers=_auth())
    assert r.status_code == 404


def test_get_price_bad_at_400(client):
    r = client.get(
        "/api/v1/price",
        params={"code": CODE, "at": "2026/09/01 09:16"},
        headers=_auth(),
    )
    assert r.status_code == 400


def test_get_price_no_data_404(client):
    r = client.get(
        "/api/v1/price",
        params={"code": "sz300750", "at": "2026-09-01T10:00:00"},
        headers=_auth(),
    )
    assert r.status_code == 404


# ================================================================
# 批量查询
# ================================================================

def test_post_prices_mixed_status(client):
    r = client.post(
        "/api/v1/prices",
        headers=_auth(),
        json={"items": [
            {"code": CODE, "at": "2026-09-01T09:16:00"},
            {"code": "sz300750"},
            {"code": "sh999999"},
        ]},
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 3
    assert items[0]["status"] == "ok"
    assert items[0]["price"] == 1450.0
    assert items[0]["at"] == "2026-09-01T09:16:00"
    assert items[1]["status"] == "error"          # 维表存在但无本地日K → no_data
    assert items[1]["error"] == "no_data"
    assert items[2]["status"] == "error"
    assert items[2]["error"] == "unknown_code"


def test_post_prices_duplicates_allowed(client):
    r = client.post(
        "/api/v1/prices",
        headers=_auth(),
        json={"items": [
            {"code": CODE, "at": "2026-09-01T10:00:00"},
            {"code": CODE, "at": "2026-09-01T10:00:00"},
        ]},
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert items[0] == items[1]
    assert items[0]["price"] == 1448.0


def test_post_prices_empty_400(client):
    r = client.post("/api/v1/prices", headers=_auth(), json={"items": []})
    assert r.status_code == 400


def test_post_prices_over_limit_400(client):
    items = [{"code": CODE} for _ in range(51)]
    r = client.post("/api/v1/prices", headers=_auth(), json={"items": items})
    assert r.status_code == 400


def test_post_prices_bad_item_code_400(client):
    r = client.post(
        "/api/v1/prices",
        headers=_auth(),
        json={"items": [{"code": "bad"}]},
    )
    assert r.status_code == 400


def test_post_prices_bad_item_at_400(client):
    r = client.post(
        "/api/v1/prices",
        headers=_auth(),
        json={"items": [{"code": CODE, "at": "not-a-time"}]},
    )
    assert r.status_code == 400


def test_post_prices_no_auth_401(client):
    r = client.post("/api/v1/prices", json={"items": [{"code": CODE}]})
    assert r.status_code == 401


# ================================================================
# token 管理 (面板会话鉴权)
# ================================================================

def test_token_create_and_use(client, monkeypatch):
    saved: dict = {}
    monkeypatch.setattr("app.secrets_store.save", lambda updates: saved.update(updates) or dict(saved))
    monkeypatch.setattr(
        "app.services.auth.is_valid_session",
        lambda token: token == "panel-session",
    )
    client.cookies.set("tf_session", "panel-session")
    r = client.post("/api/v1/token")
    assert r.status_code == 200
    new_token = r.json()["token"]
    assert new_token and new_token != TOKEN

    # 新 token 立即可用 (secrets 返回轮换后的值)
    monkeypatch.setattr(
        "app.secrets_store.load", lambda: {price_api._TOKEN_FIELD: new_token}
    )
    r2 = client.get("/api/v1/price", params={"code": CODE}, headers=_auth(new_token))
    assert r2.status_code == 200
    # 旧 token 失效
    r3 = client.get("/api/v1/price", params={"code": CODE}, headers=_auth(TOKEN))
    assert r3.status_code == 401


def test_token_revoke(client, monkeypatch):
    cleared: list = []
    monkeypatch.setattr("app.secrets_store.clear", lambda *keys: cleared.extend(keys) or {})
    monkeypatch.setattr(
        "app.services.auth.is_valid_session", lambda token: token == "panel-session"
    )
    client.cookies.set("tf_session", "panel-session")
    r = client.delete("/api/v1/token")
    assert r.status_code == 200
    assert price_api._TOKEN_FIELD in cleared


def test_token_management_requires_session(client):
    r = client.post("/api/v1/token")
    assert r.status_code == 401
    r2 = client.delete("/api/v1/token")
    assert r2.status_code == 401


# ================================================================
# 主应用认证中间件白名单
# ================================================================

def test_v1_price_bypasses_panel_session(tmp_path, monkeypatch):
    """已设面板密码但无会话 cookie: /api/v1/price* 由 Bearer 放行,
    /api/v1/token 仍需面板会话。"""
    from app.main import app
    from app.services import auth as auth_service

    monkeypatch.setattr(auth_service, "is_configured", lambda: True)
    monkeypatch.setattr(
        "app.secrets_store.load", lambda: {price_api._TOKEN_FIELD: TOKEN}
    )
    monkeypatch.setattr(
        app.state, "repo",
        SimpleNamespace(
            store=SimpleNamespace(data_dir=tmp_path),
            get_name_map=lambda symbols=None: {SYMBOL: "贵州茅台"},
            get_enriched_latest=lambda: (pl.DataFrame(), None),
        ),
        raising=False,
    )
    _write_daily(tmp_path, D1, close=1450.0)
    _write_daily(tmp_path, D2, close=1455.0)
    api_client = TestClient(app)

    # Bearer 有效 → 200 (无面板会话)
    r = api_client.get(
        "/api/v1/price", params={"code": CODE}, headers=_auth()
    )
    assert r.status_code == 200
    assert r.json()["price"] == 1455.0
    # Bearer 无效 → 路由自身 401 (非中间件会话 401, 响应体区分)
    r2 = api_client.get("/api/v1/price", params={"code": CODE})
    assert r2.status_code == 401
    # token 管理端点不在白名单 → 中间件会话 401
    r3 = api_client.post("/api/v1/token")
    assert r3.status_code == 401


# ================================================================
# 服务边界补充: _parse_at
# ================================================================

def test_parse_at():
    assert pq._parse_at("2026-09-01T09:16:00") is not None
    assert pq._parse_at("2026-09-01") is not None           # datetime.fromisoformat 宽容接受
    assert pq._parse_at("garbage") is None
    assert pq._parse_at("") is None
