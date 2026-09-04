"""market-snapshot TTL 缓存行为。

'market-snapshot' 进入前端 SSE 失效前缀后, 多端同拍重取会重复做全市场
to_dicts; 3s TTL 缓存吸收重复请求, TTL 过期后重算 (TTL < 6s 默认轮询间隔,
不牺牲 SSE 新鲜度)。
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import polars as pl


def _fake_svc(calls: list):
    """构造计数版 ScreenerService 替身: latest_date 固定, enriched 返回单行帧。"""

    class _FakeSvc:
        def __init__(self, repo):
            pass

        def latest_date(self):
            return date(2026, 1, 15)

        def _load_enriched_for_date(self, d):
            calls.append(d)
            return pl.DataFrame({
                "symbol": ["600519.SH"],
                "close": [10.0],
                "change_pct": [0.05],
            })

    return _FakeSvc


def test_market_snapshot_ttl_cache_dedupes_and_expires(monkeypatch):
    from app.api import screener as screener_api

    calls: list = []
    monkeypatch.setattr(screener_api, "ScreenerService", _fake_svc(calls))
    monkeypatch.setattr(screener_api, "_snapshot_cache", None)
    mono = {"t": 1000.0}
    monkeypatch.setattr(screener_api.time, "monotonic", lambda: mono["t"])

    req = MagicMock()
    r1 = screener_api.market_snapshot(req)
    r2 = screener_api.market_snapshot(req)
    # TTL 内命中缓存: 只计算一次, 命中返回同一份 rows
    assert len(calls) == 1
    assert r2["rows"] == r1["rows"]
    assert r2["as_of"] == "2026-01-15"

    # 过 TTL → 重算
    mono["t"] += 4.0
    r3 = screener_api.market_snapshot(req)
    assert len(calls) == 2
    assert r3["rows"][0]["symbol"] == "600519.SH"
