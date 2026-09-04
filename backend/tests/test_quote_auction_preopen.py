"""集合竞价窗口 (9:15-9:25) 实时日K构建的停牌过滤行为。

回归背景: d280dd5 把 filter_halt_days 引入实时路径 _build_daily 后, 撮合前
(9:15-9:25) 全市场股票 open/high 均为 0 (尚无开盘价), 停牌判据把整表误判为
停牌清空 → 竞价期间 enriched 不更新, 自选/监控停留在昨日数据。
"""

from __future__ import annotations

from datetime import datetime

from app.services import quote_service
from app.services.quote_service import QuoteService


def _pre_match_record(symbol: str = "600000.SH") -> dict:
    """撮合前行情记录: 无开盘价, 零成交, last_price=竞价指示价。"""
    return {
        "symbol": symbol,
        "name": "浦发银行",
        "last_price": 10.5,
        "prev_close": 10.0,
        "open": 0,
        "high": 0,
        "low": 0,
        "volume": 0,
        "amount": 0,
        "change_pct": 0.05,
        "timestamp": None,
        "session": "preopen",
    }


def test_build_daily_keeps_pre_match_rows_during_auction(monkeypatch):
    """9:15-9:25 窗口内, 撮合前零值行必须存活 (open/high/low 由 close 填充)。"""
    monkeypatch.setattr(quote_service, "cn_now", lambda: datetime(2026, 9, 4, 9, 20, 0))

    result = QuoteService._build_daily([_pre_match_record()])

    assert result.height == 1
    row = result.to_dicts()[0]
    assert row["close"] == 10.5
    # 蜡烛不从 0 开始: 零值 open/high/low 用 close 填充
    assert row["open"] == 10.5
    assert row["high"] == 10.5
    assert row["low"] == 10.5


def test_build_daily_filters_halt_rows_outside_auction(monkeypatch):
    """连续竞价时段, open/high=0 且零成交的停牌行仍被过滤 (保留 d280dd5 行为)。"""
    monkeypatch.setattr(quote_service, "cn_now", lambda: datetime(2026, 9, 4, 10, 0, 0))

    result = QuoteService._build_daily([_pre_match_record()])

    assert result.is_empty()


def test_build_daily_keeps_trading_rows_outside_auction(monkeypatch):
    """连续竞价时段, 正常成交行不受停牌过滤影响。"""
    monkeypatch.setattr(quote_service, "cn_now", lambda: datetime(2026, 9, 4, 10, 0, 0))
    rec = _pre_match_record()
    rec.update({"open": 10.2, "high": 10.6, "low": 10.1, "volume": 1000.0, "amount": 10500.0})

    result = QuoteService._build_daily([rec])

    assert result.height == 1
    assert result["close"][0] == 10.5
