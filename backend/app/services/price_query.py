"""股价时刻价查询服务 — 供 vpush「大V预估盈亏」锚定事件时刻股价。

语义契约见 docs/price-query-api.md (基于 vpush 侧设计文档 v1 适配):
  返回「不超过 at 时刻」的最近一个有效成交价格 + 该价格实际形成时刻 actual_at,
  不使用任何 at 之后的价格 (无未来信息)。

价格口径 (CONTRIBUTING §3.2 同源约束):
  - 日线取 kline_daily 的不复权原始价 (open/close);
  - 分钟K落盘为 SDK 端前复权 (qfq), 与日K不复权口径不一致, 读取后按「当日
    还原因子」还原为不复权价。当日还原因子 = 当日日K不复权 close / 当日分钟K
    末日 bar close (同日两口径同锚点, 比值即该日前复权系数; 当日无除权事件时
    为 1)。因子缺失 (无当日日K) 时该日分钟价不参与计算, 降级日线口径,
    fail-closed 不猜测。

本地分钟K时区脏数据 (2026-09-08 守卫上线前的历史分区为 UTC 墙钟):
  按与 kline_sync._enforce_minute_beijing_wallclock 同款特征判断逐日纠偏 —
  该日分钟行小时整体落在 A 股交易时段 → 北京墙钟直通; 整体呈交易时段 -8h 的
  UTC 特征 → +8h 纠偏。逐日判断 (非整表): 混布两种口径的分区目录下两类日期
  都能正确处理。无法识别 → 该日降级日线口径, 不让脏时间产出错价。

数据边界 (docs/price-query-api.md §7):
  只读本地 KlineRepository, 不触发上游数据源拉取; 本地无分钟K的交易日按
  日线边界近似 (auction/close), kind 如实标注, 不伪装成分钟精度。
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as dt_time

import polars as pl

from app.market_time import CN_TZ, cn_now, cn_today
from app.polars_guard import guarded_collect

logger = logging.getLogger(__name__)

# 代码前缀 → 交易所后缀 (内部 symbol 格式, 如 600519.SH)
_CODE_PREFIXES = {"sh": ".SH", "sz": ".SZ"}

# 北京墙钟特征时段 (A 股分钟K): 上午 09-11, 下午 13-15
_BJ_HOURS = {9, 10, 11, 13, 14, 15}
# 上述时段 -8h 的 UTC 特征: 上午 01-03, 下午 05-07
_UTC_SHIFTED_HOURS = {1, 2, 3, 5, 6, 7}

# 开盘集合竞价撮合 / 连续竞价开盘 (北京时间)。本地分钟K按 bar 起点标注
# (完整日 241 根, 09:30 起至 15:00): 09:30 bar 覆盖 [09:30, 09:31) 成交,
# 属于「不超过 at」的可用范围。
_AUCTION_DONE = dt_time(9, 25)
_SESSION_OPEN = dt_time(9, 30)
# 收盘价形成时刻 (actual_at 用)
_CLOSE_AT = dt_time(15, 0)


@dataclass(frozen=True)
class PriceAtResult:
    """单条 (code, at) 查询结果。error 非 None 时其余字段无效。"""

    code: str
    name: str | None
    at: datetime | None          # 请求时刻 (naive 北京墙钟); 最新价查询为 None
    price: float | None
    actual_at: datetime | None   # price 实际形成时刻 (naive 北京墙钟)
    kind: str | None             # trade / close / auction
    error: str | None            # unknown_code / no_data


def normalize_code(code: str) -> tuple[str, str | None]:
    """vpush 前缀码 (sh600519) → (规范化 code, None) 或 (原值, 错误码)。

    返回错误码: unknown_code (前缀不支持/位数不对)。
    """
    raw = (code or "").strip().lower()
    if len(raw) == 8 and raw[:2] in _CODE_PREFIXES and raw[2:].isdigit():
        return raw, None
    return raw or code or "", "unknown_code"


def _to_symbol(code: str) -> str | None:
    """前缀码 → 内部 symbol (600519.SH)。前缀不在支持范围 (如 bj) → None。"""
    if len(code) == 8 and code[:2] in _CODE_PREFIXES:
        return code[2:] + _CODE_PREFIXES[code[:2]]
    return None


def _parse_at(at: str) -> datetime | None:
    """解析 YYYY-MM-DDTHH:MM:SS (naive 北京墙钟)。失败返回 None (API 层 400)。"""
    try:
        return datetime.fromisoformat(at)
    except (ValueError, TypeError):
        return None


class PriceQueryService:
    """时刻价查询 — 无状态, 每请求构建; 依赖注入 KlineRepository。"""

    def __init__(self, repo) -> None:
        self._repo = repo

    # ================================================================
    # 批量入口
    # ================================================================

    def query_batch(
        self, items: list[tuple[str, datetime | None]]
    ) -> list[PriceAtResult]:
        """逐 item 独立成败; 输出顺序与输入一一对齐。

        items 的 code 为已通过 normalize_code 校验的 vpush 前缀码 (sh600519);
        内部统一转换为仓库 symbol 格式 (600519.SH) 后读盘。
        同一请求内的重复 (code, at) 只计算一次 (结果不可变, 重复查询必同值);
        日线读取按「涉及 symbol」一次 scan 完成, 避免逐 item 读盘。
        """
        # ---- 归一 + 去重计划 ----
        plan: dict[tuple[str, datetime | None], int] = {}   # (code, at) -> 首现序号
        symbols: set[str] = set()
        for code, at in items:
            if (code, at) in plan:
                continue
            plan[(code, at)] = len(plan)
            symbol = _to_symbol(code)
            if symbol is not None:
                symbols.add(symbol)

        daily = self._read_daily_window(symbols)

        # unknown_code 判定: 前缀不支持 (如 bj, 本项目无北交所数据) 或不在
        # instruments 维表 (repo.get_name_map 合并股票+ETF+指数, 进程内 memo)。
        name_map = self._repo.get_name_map(list(symbols)) if symbols else {}
        results: dict[tuple[str, datetime | None], PriceAtResult] = {}
        for (code, at), _ in plan.items():
            symbol = _to_symbol(code)
            if symbol is None or symbol not in name_map:
                results[(code, at)] = PriceAtResult(
                    code, None, at, None, None, None, "unknown_code",
                )
                continue
            results[(code, at)] = self._price_at(
                code, symbol, at, daily, name_map.get(symbol),
            )

        # ---- 回填输出顺序 ----
        return [results[(code, at)] for code, at in items]

    # ================================================================
    # 数据读取
    # ================================================================

    def _read_daily_window(self, symbols: set[str]) -> pl.DataFrame:
        """一次 scan 读取涉及 symbol 的全量本地日K (不复权)。

        列裁剪到 open/close + 停牌过滤 (filter_halt_days 同款判据);
        单次批量 ≤50 item、平均 2 年日K (~500 行/股), 全量内存无压力。
        """
        if not symbols:
            return pl.DataFrame()
        glob = str(self._repo.store.data_dir / "kline_daily" / "**" / "*.parquet")
        try:
            from app.parquet import scan_daily_parquet

            lf = (
                scan_daily_parquet(glob)
                .select("symbol", "date", "open", "close", "volume", "amount")
                .filter(pl.col("symbol").is_in(list(symbols)))
            )
            df = guarded_collect(lf)
        except Exception as e:
            logger.warning("股价查询日K读取失败: %s", e)
            return pl.DataFrame()
        if df.is_empty():
            return df
        # 停牌日过滤 (与 indicators.pipeline.filter_halt_days 同判据): 停牌行
        # close 可能被数据源填充为前收, 不过滤会把假行当有效成交。
        halted = (pl.col("open") == 0)
        if "volume" in df.columns and "amount" in df.columns:
            halted = halted | ((pl.col("volume") == 0) & (pl.col("amount") == 0))
        df = df.filter(~halted & pl.col("close").is_not_null() & (pl.col("close") > 0))
        return df.sort(["symbol", "date"])

    def _read_minute_day(self, symbol: str, trade_date: date) -> pl.DataFrame:
        """读取单 symbol 单日分钟K, 返回 (datetime, close) 按时间升序。

        时区纠偏见模块 docstring; 复权还原不在本方法做 (需当日日K, 由调用方
        传入因子)。返回空 DataFrame 表示该日无分钟数据 (调用方降级日线口径)。
        """
        base = self._repo.store.data_dir / "kline_minute"
        part = base / f"date={trade_date.isoformat()}" / "part.parquet"
        if not part.exists():
            return pl.DataFrame()
        try:
            df = pl.read_parquet(part, columns=["symbol", "datetime", "close"])
        except Exception as e:
            logger.debug("分钟K读取失败 %s %s: %s", symbol, trade_date, e)
            return pl.DataFrame()
        if df.is_empty() or "datetime" not in df.columns:
            return pl.DataFrame()
        df = df.filter(pl.col("symbol") == symbol).sort("datetime")
        if df.is_empty():
            return df

        dtype = df.schema["datetime"]
        if not isinstance(dtype, pl.Datetime):
            return pl.DataFrame()  # 非法口径, 降级
        if dtype.time_zone is not None:
            df = df.with_columns(
                pl.col("datetime").dt.convert_time_zone("Asia/Shanghai").dt.replace_time_zone(None)
            )

        # 逐日特征判断 (kline_sync 守卫的同款逻辑, 只纠偏不 fail):
        hours = df.select(pl.col("datetime").dt.hour().unique()).to_series()
        hour_set = set(hours.drop_nulls().to_list())
        bj = len(hour_set & _BJ_HOURS)
        utc_shifted = len(hour_set & _UTC_SHIFTED_HOURS)
        if utc_shifted > 0 and bj == 0:
            df = df.with_columns(pl.col("datetime") + pl.duration(hours=8))
        elif bj == 0 and utc_shifted == 0:
            return pl.DataFrame()  # 无法识别口径, 降级日线
        return df.select("datetime", "close")

    # ================================================================
    # 时刻价规则
    # ================================================================

    def _price_at(
        self,
        code: str,
        symbol: str,
        at: datetime | None,
        daily: pl.DataFrame,
        name: str | None,
    ) -> PriceAtResult:
        sym_df = daily.filter(pl.col("symbol") == symbol) if not daily.is_empty() else pl.DataFrame()

        if at is None:
            return self._latest_price(code, symbol, name, sym_df)
        if sym_df.is_empty():
            return PriceAtResult(code, name, at, None, None, None, "no_data")

        t = at.time()
        # 当日有日K (含盘中实时落盘的当日行) 才走日内分支
        day_rows = sym_df.filter(pl.col("date") == at.date())

        # ---- 1. 盘前 (含开盘集合竞价 09:15-09:25): 前一交易日收盘价 ----
        if t < _AUCTION_DONE or day_rows.is_empty():
            return self._prev_close(code, name, at, sym_df, at.date())

        # ---- 2. 09:25-09:30: 当日开盘价 (集合竞价撮合产生) ----
        if t < _SESSION_OPEN:
            open_price = day_rows["open"][0]
            if open_price is not None and math.isfinite(float(open_price)) and float(open_price) > 0:
                return PriceAtResult(
                    code, name, at, round(float(open_price), 2),
                    datetime.combine(at.date(), _AUCTION_DONE), "auction",
                    None,
                )
            # 开盘价缺失 (数据源未回填): 降级前收, 如实标注
            return self._prev_close(code, name, at, sym_df, at.date())

        # ---- 3. 盘中 09:30 之后: 分钟K (无则日K边界近似) ----
        minute = self._read_minute_day(symbol, at.date())
        if not minute.is_empty():
            factor = self._day_restore_factor(day_rows, minute)
            if factor is not None:
                # bar 起点标注: bar 覆盖 [bar_time, bar_time+1m) 的成交,
                # at 落在该区间内即视为该 bar 已发生 (at >= bar_time)。
                bars = minute.filter(pl.col("datetime") <= at)
                if not bars.is_empty():
                    last = bars.row(bars.height - 1, named=True)
                    price = float(last["close"]) * factor
                    if math.isfinite(price) and price > 0:
                        actual = last["datetime"]
                        # 午休的 at 也由「<= at 最后一根」天然覆盖:
                        # 11:30 后最后 bar 即上午末根。
                        # 命中 15:00 收盘根 → 当日收盘价, kind=close (文档口径);
                        # 其余盘中根为逐笔成交, kind=trade。
                        if actual.time() >= _SESSION_OPEN:
                            if actual.time() >= _CLOSE_AT:
                                return PriceAtResult(
                                    code, name, at, round(price, 2), actual, "close", None,
                                )
                            return PriceAtResult(
                                code, name, at, round(price, 2), actual, "trade", None,
                            )
                # 分钟序列异常 (无 >= 09:30 的有效 bar) → 落到下方日线近似

        # ---- 4. 无分钟数据: 日线边界近似 ----
        # at >= 09:30 且当日有日K: 上午取开盘价近似 (auction), 收盘后取当日收盘
        if t >= _CLOSE_AT:
            close_price = float(day_rows["close"][0])
            return PriceAtResult(
                code, name, at, round(close_price, 2),
                datetime.combine(at.date(), _CLOSE_AT), "close", None,
            )
        open_price = day_rows["open"][0]
        if open_price is not None and math.isfinite(float(open_price)) and float(open_price) > 0:
            return PriceAtResult(
                code, name, at, round(float(open_price), 2),
                datetime.combine(at.date(), _AUCTION_DONE), "auction", None,
            )
        close_price = float(day_rows["close"][0])
        return PriceAtResult(
            code, name, at, round(close_price, 2),
            datetime.combine(at.date(), _CLOSE_AT), "close", None,
        )

    def _prev_close(
        self, code: str, name: str | None, at: datetime, sym_df: pl.DataFrame, day: date
    ) -> PriceAtResult:
        """at 之前最近交易日的收盘价 (盘前/停牌/非交易日通用回溯)。"""
        prev = sym_df.filter(pl.col("date") < day)
        if prev.is_empty():
            # 本地无早于 at 的日K (at 早于上市/本地数据起点)
            return PriceAtResult(code, name, at, None, None, None, "no_data")
        last = prev.row(prev.height - 1, named=True)
        close_at = datetime.combine(last["date"], _CLOSE_AT)
        return PriceAtResult(code, name, at, round(float(last["close"]), 2), close_at, "close", None)

    def _latest_price(
        self, code: str, symbol: str, name: str | None, sym_df: pl.DataFrame
    ) -> PriceAtResult:
        """最新价 (at 省略): 实时 enriched 缓存优先, 无当日数据回退本地日K。"""
        # 实时缓存: quote_service 落盘的当日 enriched 行是最新的盘中成交
        try:
            enriched, enriched_date = self._repo.get_enriched_latest()
        except Exception:
            enriched, enriched_date = pl.DataFrame(), None
        if (
            not enriched.is_empty()
            and enriched_date == cn_today()
            and "symbol" in enriched.columns
        ):
            rows = enriched.filter(pl.col("symbol") == symbol)
            if not rows.is_empty() and "close" in rows.columns:
                price = rows["close"][0]
                if price is not None and math.isfinite(float(price)) and float(price) > 0:
                    ts = None
                    if "quote_ts" in rows.columns and rows["quote_ts"][0]:
                        try:
                            ts = datetime.fromtimestamp(int(rows["quote_ts"][0]) / 1000, tz=CN_TZ)
                        except (ValueError, TypeError, OSError):
                            ts = None
                    actual = ts.replace(tzinfo=None) if ts else cn_now().replace(tzinfo=None, microsecond=0)
                    return PriceAtResult(code, name, None, round(float(price), 2), actual, "trade", None)

        # 回退: 本地最近日K收盘价
        if sym_df.is_empty():
            return PriceAtResult(code, name, None, None, None, None, "no_data")
        last = sym_df.row(sym_df.height - 1, named=True)
        close_at = datetime.combine(last["date"], _CLOSE_AT)
        return PriceAtResult(code, name, None, round(float(last["close"]), 2), close_at, "close", None)

    def _day_restore_factor(self, day_rows: pl.DataFrame, minute: pl.DataFrame) -> float | None:
        """当日还原因子: 日K不复权 close / 当日分钟K末日 bar close。

        两口径同日同锚点 (完整交易日的分钟末日 bar 即 15:00 收盘根, 与日K
        close 同锚), 比值即该日前复权系数; 当日无除权事件时为 1。
        历史完整日锚点严格成立; 当日盘中两边都指向「最新价」(日K当日行由实时
        轮询覆写、分钟增量同频滚动), 近似成立。
        分钟末日 bar 早于 09:30 (时区纠偏失败的特征) 或值非法 → None,
        该日分钟价不参与计算 (fail-closed)。
        """
        if day_rows.is_empty() or minute.is_empty():
            return None
        daily_close = float(day_rows["close"][0])
        if not math.isfinite(daily_close) or daily_close <= 0:
            return None
        last_bar = minute.row(minute.height - 1, named=True)
        bar_close = last_bar["close"]
        bar_time = last_bar["datetime"].time() if last_bar["datetime"] is not None else None
        if bar_time is None or bar_time < _SESSION_OPEN:
            return None
        if bar_close is None or not math.isfinite(float(bar_close)) or float(bar_close) <= 0:
            return None
        factor = daily_close / float(bar_close)
        if not math.isfinite(factor) or factor <= 0:
            return None
        return factor
