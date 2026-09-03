"""实时情绪指数涨幅来源测试。

盘中指数日K分区仅在有指数监控规则时才被写盘, 若实时情绪依赖日K会让指数维
拿到缺失/陈旧涨幅, 与看板页(quote_service 实时缓存)情绪值不一致。
验证 _build_index_pct_map: 实时缓存优先(百分比口径不二次放大), 日K回退(x100)。
"""
from __future__ import annotations

from datetime import date

import polars as pl

from app.services.index_const import CORE_INDEX_SYMBOLS
from app.services.intraday_sentiment import _build_index_pct_map

TODAY = date(2026, 9, 3)


class _FakeQuoteService:
    """get_index_quotes 返回预置 DataFrame; raise 时模拟异常。"""

    def __init__(self, df: pl.DataFrame | None = None, raise_exc: bool = False):
        self._df = df
        self._raise = raise_exc
        self.calls = 0

    def get_index_quotes(self, symbols):
        self.calls += 1
        if self._raise:
            raise RuntimeError("quote unavailable")
        return self._df if self._df is not None else pl.DataFrame()


class _FakeRepo:
    """get_index_daily 按 symbol 返回小数制 change_pct (0.0123 = 1.23%)。"""

    def __init__(self, pct_by_symbol: dict[str, float] | None = None, raise_exc: bool = False):
        self._pct = pct_by_symbol or {}
        self._raise = raise_exc

    def get_index_daily(self, symbol, start, end, columns=None):
        if self._raise:
            raise RuntimeError("index daily unavailable")
        if symbol not in self._pct:
            return pl.DataFrame()
        return pl.DataFrame([{"date": TODAY, "change_pct": self._pct[symbol]}])


def _live_quotes_df(values: dict[str, float], include_symbol_col: bool = True) -> pl.DataFrame:
    rows = [
        {"symbol": s, "change_pct": p} if include_symbol_col else {"change_pct": p}
        for s, p in values.items()
    ]
    return pl.DataFrame(rows)


def test_live_quotes_take_priority_and_keep_percent_scale() -> None:
    """实时缓存命中: 百分比口径直接使用, 不查日K, 不二次 x100。"""
    qs = _FakeQuoteService(_live_quotes_df({CORE_INDEX_SYMBOLS[0]: 1.23, CORE_INDEX_SYMBOLS[1]: -0.5}))
    repo = _FakeRepo()  # 若被调用会返回空 → pct=0; calls 断言保证未走日K

    out = _build_index_pct_map(repo, TODAY, qs)

    assert qs.calls == 1
    assert out == {TODAY: {CORE_INDEX_SYMBOLS[0]: 1.23, CORE_INDEX_SYMBOLS[1]: -0.5}}


def test_live_quotes_fallback_to_daily_when_empty() -> None:
    """实时缓存为空: 回退日K, 小数制 x100 转百分比。"""
    qs = _FakeQuoteService(pl.DataFrame())
    repo = _FakeRepo({CORE_INDEX_SYMBOLS[0]: 0.0123})

    out = _build_index_pct_map(repo, TODAY, qs)

    assert out == {TODAY: {CORE_INDEX_SYMBOLS[0]: 1.23}}


def test_live_quotes_none_falls_back_to_daily() -> None:
    """quote_service 为 None (后台线程未注入等): 保持原日K行为。"""
    repo = _FakeRepo({CORE_INDEX_SYMBOLS[0]: -0.0366})

    out = _build_index_pct_map(repo, TODAY, None)

    assert out == {TODAY: {CORE_INDEX_SYMBOLS[0]: -3.66}}


def test_live_quotes_exception_falls_back_to_daily() -> None:
    """实时缓存异常: 回退日K, 不抛出。"""
    qs = _FakeQuoteService(raise_exc=True)
    repo = _FakeRepo({CORE_INDEX_SYMBOLS[0]: 0.005})

    out = _build_index_pct_map(repo, TODAY, qs)

    assert out == {TODAY: {CORE_INDEX_SYMBOLS[0]: 0.5}}


def test_live_quotes_filters_non_core_and_null_pct() -> None:
    """实时缓存混入非核心指数或空涨幅: 仅保留核心且有值的指数。"""
    foreign = "999999.XX"
    qs = _FakeQuoteService(_live_quotes_df({
        CORE_INDEX_SYMBOLS[0]: 0.8,
        CORE_INDEX_SYMBOLS[1]: None,
        foreign: 9.9,
    }))

    out = _build_index_pct_map(_FakeRepo(), TODAY, qs)

    assert out == {TODAY: {CORE_INDEX_SYMBOLS[0]: 0.8}}


def test_all_sources_empty_returns_empty_map() -> None:
    """实时缓存与日K都无数据: 返回空 map (avg_index_pct 兜底为 0)。"""
    qs = _FakeQuoteService(pl.DataFrame())
    out = _build_index_pct_map(_FakeRepo(), TODAY, qs)
    assert out == {}
