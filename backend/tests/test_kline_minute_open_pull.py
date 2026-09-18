"""minute-batch 开盘首分钟的空数据补拉回归 (b78ba99 回归的回归)。

指数分钟 K 无本地存储, 分时图完全依赖本端点每轮实时补拉。完整性判定的
`fresh_floor = max(0, expected - 2)` 根数容差在开盘 9:31/9:32 (expected<=2)
会把「本地为空」误判为「本地已完整」(0 >= 0), 该轮不发起任何拉取, 指数页
分时图空白约 3 分钟 — 用户表现为「开盘前 5 分钟不显示内容」。

b78ba99 之前的旧逻辑是 `expected > 0 and (sub.is_empty() or ...)` 即
「空 + 已开盘 → 必补拉」; 本组测试钉住该不变量恢复, 同时保住容差的本意
(本地真实跟上时刻的数据不重拉, 防止修过头变成每轮全天重拉)。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock
from unittest.mock import MagicMock

import polars as pl
import pytest

from app.api import kline as kline_api
from app.market_time import CN_TZ
from app.services import preferences, trading_day

TODAY = date(2026, 9, 15)  # 周二, 交易日
INDEX_SYMBOL = "000001.SH"
STOCK_SYMBOL = "600519.SH"


def _bars(symbol: str, dts: list[datetime]) -> pl.DataFrame:
    """canonical 8 列分钟K帧。"""
    n = len(dts)
    return pl.DataFrame({
        "symbol": [symbol] * n,
        "datetime": dts,
        "open": [10.0] * n, "high": [10.1] * n, "low": [9.9] * n, "close": [10.0] * n,
        "volume": [100.0] * n, "amount": [1000.0] * n,
    })


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch):
    """钉住交易日/探针/压缩开关; 返回 (设置北京时刻的函数, 批量拉取 mock)。"""
    monkeypatch.setattr(kline_api, "cn_today", lambda: TODAY)
    monkeypatch.setattr(trading_day, "is_trading_day", lambda now=None: True)
    monkeypatch.setattr(preferences, "get_minute_batch_compress", lambda: False)
    sync = MagicMock(return_value=pl.DataFrame())
    monkeypatch.setattr(kline_api.kline_sync, "sync_minute_batch", sync)

    def set_time(hour: int, minute: int) -> None:
        now = datetime(TODAY.year, TODAY.month, TODAY.day, hour, minute, tzinfo=CN_TZ)
        monkeypatch.setattr(kline_api, "cn_now", lambda: now)

    return set_time, sync


def _request(local_df: pl.DataFrame, *, index_symbols: set[str] | None = None,
             date_str: str | None = None, latest_daily: date | None = TODAY) -> MagicMock:
    """get_minute_batch 的最小 mock: 指数恒无本地存储, 拉取结果不落盘断言由测试负责。"""
    mock_repo = MagicMock()
    mock_repo.get_etf_symbol_set.return_value = set()
    mock_repo.get_index_symbol_set.return_value = (
        index_symbols if index_symbols is not None else {INDEX_SYMBOL}
    )
    mock_repo.get_minute_batch.return_value = local_df
    mock_repo.latest_daily_date.return_value = latest_daily
    mock_repo._write_lock = Lock()
    mock_repo.store.data_dir = Path("data")

    mock_capset = MagicMock()
    mock_capset.has.return_value = True
    mock_capset.limits.return_value = None

    mock_request = MagicMock()
    mock_request.app.state.repo = mock_repo
    mock_request.app.state.capabilities = mock_capset
    mock_request.headers = {}
    body = {"symbols": [INDEX_SYMBOL]}
    if date_str:
        body["date"] = date_str
    mock_request_body = body
    return mock_request, mock_request_body


def _writes_spy(monkeypatch: pytest.MonkeyPatch) -> list[pl.DataFrame]:
    writes: list[pl.DataFrame] = []
    monkeypatch.setattr(kline_api.kline_sync, "_write_minute_partition",
                        lambda df, d: writes.append(df))
    return writes


# ---------- 复现用例: 开盘首分钟空数据必须补拉 ----------


@pytest.mark.parametrize(("hour", "minute"), [(9, 31), (9, 32)])
def test_open_first_minutes_empty_index_pulls(clock, hour, minute, monkeypatch) -> None:
    """9:31/9:32 指数本地为空 → 进 full_pull 实时拉取 (未修复时被容差误判 fresh)。

    指数无本地存储, 分时图只活在本端点的补拉上; 这是用户看到的
    「开盘前几分钟不显示内容」的直接复现用例。
    """
    set_time, sync = clock
    set_time(hour, minute)
    writes = _writes_spy(monkeypatch)

    req, body = _request(pl.DataFrame())
    kline_api.get_minute_batch(req, body)

    assert sync.call_count == 1
    call = sync.call_args
    assert call.kwargs.get("asset_type") == "index"      # 指数走 index 路由
    assert list(call.args[0]) == [INDEX_SYMBOL]
    assert call.kwargs.get("start_time") == datetime(    # 全天窗口 (full_pull)
        TODAY.year, TODAY.month, TODAY.day, 9, 25)
    assert writes == []                                   # 指数补拉不落盘


def test_open_first_minute_pull_result_returned(clock, monkeypatch) -> None:
    """9:31 补拉到 1 根 → 立即返回给前端, 图上出现第一个点。"""
    set_time, sync = clock
    set_time(9, 31)
    sync.return_value = _bars(INDEX_SYMBOL, [datetime(TODAY.year, TODAY.month, TODAY.day, 9, 31)])

    req, body = _request(pl.DataFrame())
    payload = kline_api.get_minute_batch(req, body)

    rows = payload["data"][INDEX_SYMBOL]
    assert len(rows) == 1
    assert rows[0]["datetime"] == datetime(TODAY.year, TODAY.month, TODAY.day, 9, 31)


# ---------- 保住容差本意: 本地真实跟上时刻的数据不重拉 ----------


def test_open_local_caught_up_skips_pull(clock, monkeypatch) -> None:
    """9:33 本地已有 2 根 (>= fresh_floor=1) → 直读本地, 零请求。

    「空不算 fresh」不能修过头: 本地确实跟上了时刻的标的仍须命中容差,
    否则恢复成 b78ba99 要解决的「90% 处冻结尾巴/每轮重拉」。
    """
    set_time, sync = clock
    set_time(9, 33)
    writes = _writes_spy(monkeypatch)
    local = _bars(INDEX_SYMBOL, [
        datetime(TODAY.year, TODAY.month, TODAY.day, 9, 31),
        datetime(TODAY.year, TODAY.month, TODAY.day, 9, 32),
    ])

    req, body = _request(local)
    payload = kline_api.get_minute_batch(req, body)

    sync.assert_not_called()
    assert writes == []
    assert len(payload["data"][INDEX_SYMBOL]) == 2


def test_open_local_single_bar_matches_expected1(clock, monkeypatch) -> None:
    """9:31 本地恰有 1 根 (== expected) → 同样直读本地不重拉 (容差边界)。"""
    set_time, sync = clock
    set_time(9, 31)
    local = _bars(INDEX_SYMBOL, [datetime(TODAY.year, TODAY.month, TODAY.day, 9, 31)])

    req, body = _request(local)
    payload = kline_api.get_minute_batch(req, body)

    sync.assert_not_called()
    assert len(payload["data"][INDEX_SYMBOL]) == 1


# ---------- 开盘前的设计语义维持 ----------


def test_preopen_expected_zero_skips_pull(clock) -> None:
    """9:2x 未开盘 (expected=0) → 不拉取 (盘前空请求纯浪费), awaiting_open=True。"""
    set_time, sync = clock
    set_time(9, 26)

    req, body = _request(pl.DataFrame())
    payload = kline_api.get_minute_batch(req, body)

    sync.assert_not_called()
    assert payload["data"] == {}
    assert payload["awaiting_open"] is True


def test_open_awaiting_open_flag_window(clock) -> None:
    """awaiting_open 窗口: 9:15 起为 True; 开盘数据正常后 (9:40) 为 False。"""
    set_time, _ = clock

    req, body = _request(pl.DataFrame())
    set_time(9, 16)
    assert kline_api.get_minute_batch(req, body)["awaiting_open"] is True

    set_time(9, 40)
    assert kline_api.get_minute_batch(req, body)["awaiting_open"] is False


def test_after_close_awaiting_open_false(clock) -> None:
    """收盘后 (15:30) 的空数据是真实缺失, awaiting_open=False (不进等待窗)。"""
    set_time, _ = clock
    set_time(15, 30)

    req, body = _request(pl.DataFrame())
    payload = kline_api.get_minute_batch(req, body)

    assert payload["awaiting_open"] is False


def test_past_date_awaiting_open_false(clock) -> None:
    """历史日期 (date 参数显式指定昨天) 不进等待窗 — 空就是没有数据。"""
    set_time, _ = clock
    set_time(9, 20)

    yesterday = (TODAY - timedelta(days=1)).isoformat()
    req, body = _request(pl.DataFrame(), date_str=yesterday)
    payload = kline_api.get_minute_batch(req, body)

    assert payload["awaiting_open"] is False


# ---------- 尾部增量语义不回归 ----------


def test_open_local_partial_tail_uses_incremental(clock) -> None:
    """9:35 本地 1 根连续 (首根 09:31, 无洞, < fresh_floor) → 走尾部增量
    (从最后一根本身重拉), 不是 full_pull 的 09:25 全天窗口。

    指数无本地存储不会出现该状态, 用股票语义验证: 「空不算 fresh」的收紧
    不得顺带破坏「仅尾部落后 → 增量」的三态分类。
    """
    set_time, sync = clock
    set_time(9, 35)
    local = _bars(STOCK_SYMBOL, [datetime(TODAY.year, TODAY.month, TODAY.day, 9, 31)])

    req, body = _request(local, index_symbols=set())
    body["symbols"] = [STOCK_SYMBOL]
    kline_api.get_minute_batch(req, body)

    assert sync.call_count == 1
    assert sync.call_args.kwargs.get("start_time") == datetime(
        TODAY.year, TODAY.month, TODAY.day, 9, 31)   # 最后一根本身 (动态K覆盖)


# ---------- 股票路径不受辐射 ----------


def test_stock_empty_at_open_also_pulls(clock, monkeypatch) -> None:
    """股票本地为空 + 已开盘 → 同样进 full_pull (恢复 b78ba99 前的旧语义;
    股票有本地落盘, 下一轮起命中本地, 无请求量恶化)。"""
    set_time, sync = clock
    set_time(9, 31)

    req, body = _request(pl.DataFrame(), index_symbols=set())
    body["symbols"] = [STOCK_SYMBOL]
    kline_api.get_minute_batch(req, body)

    assert sync.call_count == 1
    assert sync.call_args.kwargs.get("asset_type") == "stock"
    assert list(sync.call_args.args[0]) == [STOCK_SYMBOL]
