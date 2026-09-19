"""股价时刻价查询服务测试 — docs/price-query-api.md 时刻价规则全边界覆盖。

日期用 2026-09-01(周二)/2026-08-31(周一) 等绝对过去交易日, 与仓库既有测试
风格一致。分钟K夹具同时覆盖北京墙钟与历史 UTC 墙钟两类分区 (本地数据实测
2026-09-08 前的分区为 UTC 口径)。
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import polars as pl
import pytest

from app.services import price_query as pq
from app.tickflow.repository import DataStore, KlineRepository

SYMBOL = "600519.SH"
CODE = "sh600519"
D1, D2 = date(2026, 8, 31), date(2026, 9, 1)   # 周一 / 周二


def _write_daily(data_dir: Path, day: date, rows: list[tuple[str, float, float, float]]) -> None:
    """rows: (symbol, open, close, volume)。"""
    part = data_dir / "kline_daily" / f"date={day.isoformat()}"
    part.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": [r[0] for r in rows],
        "date": [day for _ in rows],
        "open": [r[1] for r in rows],
        "close": [r[2] for r in rows],
        "volume": [r[3] for r in rows],
        "amount": [r[3] * 1000.0 for r in rows],
        "quote_ts": [0 for _ in rows],
    }).write_parquet(part / "part.parquet")


def _full_minute_bars(prices: dict[tuple[int, int], float]) -> list[tuple[int, int, float]]:
    """构造完整交易日分钟序列 (bar 起点标注: 09:30..11:30 + 13:00..15:00, 241 根)。

    prices 按 (hour, minute) 时刻给关键价格, 其余默认 1449.0。
    """
    bars: list[tuple[int, int, float]] = []
    t = 9 * 60 + 30
    while t <= 11 * 60 + 30:
        bars.append((t // 60, t % 60, prices.get((t // 60, t % 60), 1449.0)))
        t += 1
    t = 13 * 60
    while t <= 15 * 60:
        bars.append((t // 60, t % 60, prices.get((t // 60, t % 60), 1449.0)))
        t += 1
    return bars


def _write_minute(data_dir: Path, day: date, bars: list[tuple[int, int, float]], *, utc: bool = False) -> None:
    """bars: (hour, minute, close)。utc=True 写 UTC 墙钟 (历史脏分区口径)。"""
    part = data_dir / "kline_minute" / f"date={day.isoformat()}"
    part.mkdir(parents=True, exist_ok=True)
    shift = 8 if utc else 0
    pl.DataFrame({
        "symbol": [SYMBOL for _ in bars],
        "datetime": [
            datetime(day.year, day.month, day.day, ((h - shift) % 24), m) for h, m, _ in bars
        ],
        "close": [c for _, _, c in bars],
    }).write_parquet(part / "part.parquet")


@pytest.fixture()
def repo(tmp_path: Path) -> KlineRepository:
    # 两日日K: D1 收 1450, D2 开 1448 收 1455
    _write_daily(tmp_path, D1, [(SYMBOL, 1452.0, 1450.0, 32000.0)])
    _write_daily(tmp_path, D2, [(SYMBOL, 1448.0, 1455.0, 28000.0)])
    return KlineRepository(DataStore(tmp_path))


class _Repo:
    """服务最小依赖面 (store.data_dir + get_name_map + get_enriched_latest)。"""

    def __init__(self, data_dir: Path):
        self.store = type("S", (), {"data_dir": data_dir})()
        # 300750.SZ 在维表但夹具不写其日K → no_data 路径
        self._names = {SYMBOL: "贵州茅台", "300750.SZ": "宁德时代"}

    def get_name_map(self, symbols=None):
        if symbols is None:
            return dict(self._names)
        return {s: n for s, n in self._names.items() if s in set(symbols)}

    def get_enriched_latest(self):
        return pl.DataFrame(), None


def _svc(tmp_path: Path) -> pq.PriceQueryService:
    return pq.PriceQueryService(_Repo(tmp_path))


def _at(day: date, h: int, m: int) -> datetime:
    return datetime(day.year, day.month, day.day, h, m)


# ================================================================
# normalize_code
# ================================================================

def test_normalize_code():
    assert pq.normalize_code("sh600519") == ("sh600519", None)
    assert pq.normalize_code("SZ300750") == ("sz300750", None)
    assert pq.normalize_code("bj430047")[1] == "unknown_code"
    assert pq.normalize_code("sh60051")[1] == "unknown_code"
    assert pq.normalize_code("xx600519")[1] == "unknown_code"
    assert pq.normalize_code("600519")[1] == "unknown_code"


# ================================================================
# 时刻价规则 — 时段边界
# ================================================================

def test_auction_window_uses_prev_close(repo, tmp_path):
    """09:15-09:25 集合竞价期: 当日尚无成交, 取前一日收盘 (kind=close)。"""
    svc = _svc(tmp_path)
    res = svc.query_batch([(CODE, _at(D2, 9, 16))])[0]
    assert res.error is None
    assert res.price == 1450.0
    assert res.kind == "close"
    assert res.actual_at == datetime(2026, 8, 31, 15, 0)


def test_between_auction_and_open_uses_day_open(repo, tmp_path):
    """09:25-09:30: 开盘价已撮合产生 (kind=auction)。"""
    svc = _svc(tmp_path)
    res = svc.query_batch([(CODE, _at(D2, 9, 27))])[0]
    assert res.error is None
    assert res.price == 1448.0
    assert res.kind == "auction"
    assert res.actual_at == datetime(2026, 9, 1, 9, 25)


def test_continuous_session_uses_minute_bar(repo, tmp_path):
    """09:30 后有分钟K: 取 <= at 最后一根有成交的 bar (kind=trade)。
    bar 起点标注: 09:34 bar 覆盖 [09:34, 09:35) 成交。"""
    _write_minute(tmp_path, D2, _full_minute_bars({
        (9, 30): 1448.5,
        (9, 34): 1450.2,
        (15, 0): 1455.0,   # 收盘 bar = 日K close
    }))
    svc = _svc(tmp_path)
    res = svc.query_batch([(CODE, _at(D2, 9, 34))])[0]
    assert res.error is None
    assert res.price == 1450.2
    assert res.kind == "trade"
    assert res.actual_at == datetime(2026, 9, 1, 9, 34)


def test_minute_bar_at_exact_boundary(repo, tmp_path):
    """at 恰好等于某 bar 起点时刻: 该 bar 正在发生 (at >= bar_time 即含)。"""
    _write_minute(tmp_path, D2, _full_minute_bars({
        (9, 30): 1448.5, (9, 31): 1449.5, (15, 0): 1455.0,
    }))
    svc = _svc(tmp_path)
    res = svc.query_batch([(CODE, _at(D2, 9, 31))])[0]
    assert res.price == 1449.5
    assert res.kind == "trade"


def test_lunch_break_uses_morning_last_bar(repo, tmp_path):
    """午间休市 11:30-13:00: 当日上午最后一笔成交 (11:30 bar)。"""
    _write_minute(tmp_path, D2, _full_minute_bars({
        (9, 30): 1448.5, (11, 30): 1447.0, (15, 0): 1455.0,
    }))
    svc = _svc(tmp_path)
    res = svc.query_batch([(CODE, _at(D2, 12, 30))])[0]
    assert res.price == 1447.0
    assert res.kind == "trade"
    assert res.actual_at == datetime(2026, 9, 1, 11, 30)


def test_after_close_uses_day_close(repo, tmp_path):
    """15:00 后: 当日收盘价 (kind=close)。"""
    _write_minute(tmp_path, D2, _full_minute_bars({
        (9, 30): 1448.5, (15, 0): 1455.0,
    }))
    svc = _svc(tmp_path)
    res = svc.query_batch([(CODE, _at(D2, 16, 0))])[0]
    assert res.price == 1455.0
    assert res.kind == "close"
    assert res.actual_at == datetime(2026, 9, 1, 15, 0)


def test_no_minute_data_degrades_to_daily(repo, tmp_path):
    """本地无分钟K: 盘中降级当日开盘价近似 (kind=auction, 如实标注);
    收盘后取当日收盘 (kind=close)。"""
    svc = _svc(tmp_path)
    res = svc.query_batch([(CODE, _at(D2, 10, 30))])[0]
    assert res.price == 1448.0
    assert res.kind == "auction"
    res2 = svc.query_batch([(CODE, _at(D2, 22, 0))])[0]
    assert res2.price == 1455.0
    assert res2.kind == "close"


# ================================================================
# 时刻价规则 — 跨日 / 停牌 / 数据边界
# ================================================================

def test_non_trading_day_rolls_back(repo, tmp_path):
    """非交易日 (周末): 之前最近交易日收盘价。D2 为周二 → 周六查回 D2 收盘。"""
    sat = date(2026, 9, 5)
    svc = _svc(tmp_path)
    res = svc.query_batch([(CODE, _at(sat, 10, 0))])[0]
    assert res.price == 1455.0
    assert res.kind == "close"
    assert res.actual_at == datetime(2026, 9, 1, 15, 0)


def test_halt_day_crosses_back(repo, tmp_path):
    """at 当日停牌 (日K无该日行): 向前回溯到最近有成交日收盘。"""
    # D3 (周三) 无日K = 停牌/无数据日
    d3 = date(2026, 9, 2)
    svc = _svc(tmp_path)
    res = svc.query_batch([(CODE, _at(d3, 10, 0))])[0]
    assert res.price == 1455.0
    assert res.kind == "close"
    assert res.actual_at == datetime(2026, 9, 1, 15, 0)


def test_halt_row_filtered_not_treated_as_valid(repo, tmp_path):
    """停牌日残留行 (open=0, close 被数据源填充为前收): 过滤后回溯前一日。"""
    d3 = date(2026, 9, 2)
    _write_daily(tmp_path, d3, [(SYMBOL, 0.0, 1455.0, 0.0)])  # open=0, vol=0 假行
    svc = _svc(tmp_path)
    res = svc.query_batch([(CODE, _at(d3, 10, 0))])[0]
    assert res.price == 1455.0
    assert res.kind == "close"
    assert res.actual_at == datetime(2026, 9, 1, 15, 0)


def test_before_listing_no_data(repo, tmp_path):
    """at 早于本地任何日K: no_data (而非错误价格)。"""
    svc = _svc(tmp_path)
    res = svc.query_batch([(CODE, _at(date(2020, 1, 1), 10, 0))])[0]
    assert res.error == "no_data"
    assert res.price is None


def test_unknown_code(repo, tmp_path):
    svc = _svc(tmp_path)
    res = svc.query_batch([("sh999999", _at(D2, 10, 0))])[0]
    assert res.error == "unknown_code"


# ================================================================
# 复权还原
# ================================================================

def test_minute_restored_to_raw_price(repo, tmp_path):
    """分钟K为前复权存储: 除权日 qfq 分钟价 ≠ 原始价时, 按当日日K/末日bar 比值
    还原为不复权价 (与日K收盘同锚 → 收盘时刻查价 == 日K close)。"""
    # D2 发生除权: 日K原始 close=730, 分钟K(qfq) 15:00 bar=728.5 → 因子 730/728.5
    _write_daily(tmp_path, D2, [(SYMBOL, 728.0, 730.0, 28000.0)])
    _write_minute(tmp_path, D2, _full_minute_bars({
        (9, 30): 728.2, (9, 31): 728.4, (15, 0): 728.5,
    }))
    svc = _svc(tmp_path)
    # 收盘时刻: 还原后应等于日K close 730.0
    res = svc.query_batch([(CODE, _at(D2, 15, 0))])[0]
    assert res.price == 730.0
    # 盘中 9:31: 728.4 * (730/728.5) ≈ 728.9 (不复权口径)
    res2 = svc.query_batch([(CODE, _at(D2, 9, 31))])[0]
    assert res2.price == pytest.approx(728.4 * 730.0 / 728.5, abs=0.01)
    assert res2.kind == "trade"


def test_minute_factor_missing_degrades(repo, tmp_path):
    """当日无日K (分钟K 孤立): 因子缺失 → 降级前收, 不用无法定标的分钟价。"""
    d3 = date(2026, 9, 2)
    _write_minute(tmp_path, d3, _full_minute_bars({(9, 30): 1456.0}))
    svc = _svc(tmp_path)
    res = svc.query_batch([(CODE, _at(d3, 10, 0))])[0]
    assert res.price == 1455.0  # D2 收盘
    assert res.kind == "close"


# ================================================================
# 分钟K时区脏数据纠偏
# ================================================================

def test_utc_shifted_minute_partition_corrected(repo, tmp_path):
    """历史 UTC 墙钟分区 (2026-09-08 守卫上线前): +8h 纠偏后正常参与计算。"""
    _write_minute(tmp_path, D2, _full_minute_bars({
        (9, 30): 1448.5, (9, 32): 1450.5, (15, 0): 1455.0,
    }), utc=True)
    svc = _svc(tmp_path)
    res = svc.query_batch([(CODE, _at(D2, 9, 32))])[0]
    assert res.error is None
    assert res.price == 1450.5
    assert res.kind == "trade"


# ================================================================
# 最新价 (at 省略)
# ================================================================

def test_latest_price_falls_back_to_local_daily(repo, tmp_path):
    """无实时缓存: 回退本地最近日K收盘。"""
    svc = _svc(tmp_path)
    res = svc.query_batch([(CODE, None)])[0]
    assert res.error is None
    assert res.price == 1455.0
    assert res.kind == "close"
    assert res.actual_at == datetime(2026, 9, 1, 15, 0)
    assert res.at is None


def test_latest_price_uses_enriched_today(tmp_path):
    """有当日实时 enriched: 用最新成交 (kind=trade, actual_at 取 quote_ts)。"""
    _write_daily(tmp_path, D1, [(SYMBOL, 1452.0, 1450.0, 32000.0)])
    _write_daily(tmp_path, D2, [(SYMBOL, 1448.0, 1455.0, 28000.0)])

    class _LiveRepo(_Repo):
        def get_enriched_latest(self):
            ts_ms = int(datetime(2026, 9, 1, 10, 47, 12).timestamp() * 1000)
            df = pl.DataFrame({
                "symbol": [SYMBOL], "close": [1453.6], "quote_ts": [ts_ms],
            })
            return df, D2

    import unittest.mock
    with unittest.mock.patch("app.services.price_query.cn_today", return_value=D2):
        svc = pq.PriceQueryService(_LiveRepo(tmp_path))
        res = svc.query_batch([(CODE, None)])[0]
    assert res.price == 1453.6
    assert res.kind == "trade"
    assert res.actual_at == datetime(2026, 9, 1, 10, 47, 12)


# ================================================================
# 批量行为
# ================================================================

def test_batch_order_and_mixed_status(repo, tmp_path):
    """输出顺序与请求一一对齐; 逐 item 独立成败 (unknown_code / no_data 不拖垮整单);
    bj 前缀 (服务层公共入口直接调用) 也不崩溃, 判 unknown_code。"""
    _write_minute(tmp_path, D2, _full_minute_bars({(9, 30): 1449.0, (15, 0): 1455.0}))
    svc = _svc(tmp_path)
    items = [
        (CODE, _at(D2, 9, 30)),
        ("sh999999", _at(D2, 9, 31)),      # 不在维表
        (CODE, None),                       # 最新价
        ("sz300750", _at(D2, 10, 0)),       # 无本地数据
        ("bj430047", _at(D2, 10, 0)),       # 前缀不支持
    ]
    results = svc.query_batch(items)
    assert [r.code for r in results] == [
        "sh600519", "sh999999", "sh600519", "sz300750", "bj430047",
    ]
    assert results[0].price == 1449.0 and results[0].kind == "trade"
    assert results[1].error == "unknown_code"
    assert results[2].price == 1455.0
    assert results[3].error == "no_data"
    assert results[4].error == "unknown_code"


def test_batch_dedup_same_query(repo, tmp_path):
    """同一 (code, at) 重复: 只计算一次, 结果一致。"""
    svc = _svc(tmp_path)
    results = svc.query_batch([(CODE, _at(D2, 10, 0)), (CODE, _at(D2, 10, 0))])
    assert results[0].price == results[1].price == 1448.0
