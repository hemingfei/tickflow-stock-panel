"""免登录实时环境情绪公开 API (/api/public/env/*) 测试:
与站内端点同口径复用、认证白名单放行、compute 计算入口不公开。"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import regime_intraday as regime_api
from app.api import sentiment_intraday as sentiment_api
from app.services import intraday_regime, intraday_sentiment


@pytest.fixture
def client(tmp_path):
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    for r in (
        regime_api.router,
        regime_api.public_router,
        sentiment_api.router,
        sentiment_api.public_router,
    ):
        app.include_router(r)
    return TestClient(app), tmp_path


def _seed(tmp_path):
    day = date(2026, 9, 2)
    intraday_regime.save_intraday_regime(
        tmp_path,
        pl.DataFrame([{"timestamp": 1, "time": "09:35", "score": 66, "state": "lean_strong"}]),
        day,
    )
    intraday_sentiment.save_intraday_sentiment(
        tmp_path,
        pl.DataFrame([{"timestamp": 1, "time": "09:35", "emotion_score": 58, "emotion_label": "偏暖"}]),
        day,
    )


def test_public_env_same_payload_as_internal(client):
    """公开端点与站内端点同一实现同一口径; 无数据日返回空列表而非报错。"""
    api_client, tmp_path = client
    _seed(tmp_path)

    for internal, public in (
        ("/api/regime/intraday/history?target_date=2026-09-02",
         "/api/public/env/regime/history?target_date=2026-09-02"),
        ("/api/sentiment/intraday/history?target_date=2026-09-02",
         "/api/public/env/sentiment/history?target_date=2026-09-02"),
    ):
        assert api_client.get(public).json() == api_client.get(internal).json()
        assert api_client.get(public).json()["data"][0]["time"] == "09:35"

    assert api_client.get("/api/public/env/regime/dates").json() == {"dates": ["2026-09-02"]}
    assert api_client.get("/api/public/env/sentiment/dates").json() == {"dates": ["2026-09-02"]}
    assert api_client.get("/api/public/env/regime/history?target_date=2026-09-03").json()["data"] == []
    assert api_client.get("/api/public/env/sentiment/history?target_date=2026-09-02").json()["data"][0]["emotion_score"] == 58


def test_public_env_whitelist_and_compute_not_public(tmp_path, monkeypatch):
    """已设密码时: 公开只读端点免会话可访问; 站内端点与 compute 计算入口仍需登录。"""
    from app.main import app
    from app.services import auth as auth_service

    monkeypatch.setattr(auth_service, "is_configured", lambda: True)
    monkeypatch.setattr(
        app.state, "repo", SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)), raising=False)
    api_client = TestClient(app)

    assert api_client.get("/api/public/env/regime/dates").status_code == 200
    assert api_client.get("/api/public/env/sentiment/dates").status_code == 200
    assert api_client.get("/api/regime/intraday/dates").status_code == 401
    assert api_client.get("/api/sentiment/intraday/dates").status_code == 401
    # 公开空间只有只读 GET 端点: compute 路由不存在 (主应用 SPA catch-all 为 GET-only,
    # POST 部分匹配得 405), 站内 compute 触发服务端计算需登录
    assert api_client.post("/api/public/env/regime/compute").status_code in (404, 405)
    assert api_client.post("/api/public/env/sentiment/compute").status_code in (404, 405)
    assert api_client.post("/api/regime/intraday/compute").status_code == 401
    assert api_client.post("/api/sentiment/intraday/compute").status_code == 401
