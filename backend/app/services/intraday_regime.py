"""实时环境计算服务 - 分钟级环境综合分及四个维度数据。

交易日实时行情期间，每分钟获取一次完整的环境指标四维数据，保存记录。
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import polars as pl

from app.services import regime_builder
from app.market_time import cn_now, cn_today
from app.tickflow.repository import enriched_dirname
from app.indicators.pipeline import compute_indicators, compute_limit_signals, compute_signals
from app.parquet import scan_enriched_parquet

logger = logging.getLogger(__name__)

# 数据存储目录
INTRADAY_REGIME_DIR = "regime_intraday"

# A 股交易时段检查（与 intraday_sentiment 保持一致）
MORNING_START = (9, 30)
MORNING_END = (11, 30)
AFTERNOON_START = (13, 0)
AFTERNOON_END = (15, 0)
AUCTION_START = (9, 15)
AUCTION_END = (9, 25)
POST_CLOSE_END = (15, 5)


def is_trading_time(dt: datetime | None = None) -> bool:
    """检查是否在交易时段内。"""
    dt = dt or cn_now()
    hour, minute = dt.hour, dt.minute
    
    # 检查是否是工作日（周一到周五）
    if dt.weekday() >= 5:
        return False
    
    # 集合竞价 9:15-9:25
    if (hour == AUCTION_START[0] and minute >= AUCTION_START[1]) or (hour > AUCTION_START[0] and hour < AUCTION_END[0]) or (hour == AUCTION_END[0] and minute <= AUCTION_END[1]):
        return True
    
    # 上午时段 9:30-11:30
    if (hour == MORNING_START[0] and minute >= MORNING_START[1]) or (hour > MORNING_START[0] and hour < MORNING_END[0]) or (hour == MORNING_END[0] and minute <= MORNING_END[1]):
        return True
    
    # 下午时段 13:00-15:00
    if (hour == AFTERNOON_START[0] and minute >= AFTERNOON_START[1]) or (hour > AFTERNOON_START[0] and hour < AFTERNOON_END[0]) or (hour == AFTERNOON_END[0] and minute <= AFTERNOON_END[1]):
        return True
    
    # 收盘后 15:00-15:05
    if (hour == 15 and minute <= 5):
        return True
    
    return False


def should_record_now(dt: datetime | None = None) -> bool:
    """判断当前时刻是否应该记录环境数据。"""
    dt = dt or cn_now()
    hour, minute, second = dt.hour, dt.minute, dt.second
    
    # 首先检查是否在交易时段内
    if not is_trading_time(dt):
        return False
    
    # 特定秒点记录（精确匹配）
    special_times = [
        (9, 15, 10), (9, 15, 20),
        (9, 25, 10), (9, 25, 20),
        (9, 30, 10), (9, 30, 20),
        (15, 0, 10), (15, 0, 20),
    ]
    if (hour, minute, second) in special_times:
        return True
    
    # 9:15-9:25 期间每一分钟的 0 秒左右
    if (hour == 9 and 15 <= minute <= 25) and second < 10:
        return True
    
    # 15:00-15:05 期间每一分钟的 0 秒左右
    if (hour == 15 and minute <= 5) and second < 10:
        return True
    
    # 常规交易时段（9:30-11:30 和 13:00-15:00）每一分钟的 0 秒左右
    if ((hour == 9 and minute >= 30) or (10 <= hour <= 10) or (hour == 11 and minute <= 30) or 
        (13 <= hour <= 14) or (hour == 15 and minute == 0)) and second < 10:
        return True
    
    return False


def intraday_regime_path(data_dir: Path, target_date: date | None = None) -> Path:
    """获取指定日期的分钟级环境数据存储路径。"""
    target_date = target_date or cn_today()
    date_str = target_date.strftime("%Y-%m-%d")
    return data_dir / INTRADAY_REGIME_DIR / f"{date_str}.parquet"


def load_intraday_regime(data_dir: Path, target_date: date | None = None) -> pl.DataFrame:
    """加载指定日期的分钟级环境数据。"""
    path = intraday_regime_path(data_dir, target_date)
    if not path.exists():
        return pl.DataFrame()
    try:
        return pl.read_parquet(path)
    except Exception as e:
        logger.warning("load_intraday_regime failed: %s", e)
        return pl.DataFrame()


def save_intraday_regime(data_dir: Path, df: pl.DataFrame, target_date: date | None = None) -> None:
    """保存分钟级环境数据。"""
    if df.is_empty():
        return
    path = intraday_regime_path(data_dir, target_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(path)


def append_intraday_regime(data_dir: Path, record: dict[str, Any], target_date: date | None = None) -> None:
    """追加单条分钟级环境记录。"""
    existing = load_intraday_regime(data_dir, target_date)
    new_record = pl.DataFrame([record])
    
    if existing.is_empty():
        combined = new_record
    else:
        # 移除已存在的相同时间戳记录
        existing = existing.filter(pl.col("timestamp") != record["timestamp"])
        combined = pl.concat([existing, new_record], how="vertical_relaxed")
    
    combined = combined.sort("timestamp")
    save_intraday_regime(data_dir, combined, target_date)


def _depth_fake_limit_up(depth_service, target_date: date) -> int:
    """五档假涨停家数(价格在涨停价但未封住), 供涨停数对齐情绪/看板封板口径。

    depth 不可用、未就绪或读取失败时返回 0(不修正), 与情绪链路的
    sealed_ready=False 降级语义一致; 不让 depth 异常拖垮整条分钟记录。
    """
    if depth_service is None:
        return 0
    try:
        return depth_service.fake_limit_count(target_date, is_down=False)
    except Exception as e:
        logger.warning("depth fake limit count failed: %s", e)
        return 0


def _compute_intraday_regime_impl(repo, depth_service=None) -> dict[str, Any] | None:
    """计算当前时刻的实时环境指标。"""
    try:
        now = cn_now()
        target_date = now.date()
        
        # 加载完整的 enriched 数据
        enriched_dir = repo.store.data_dir / enriched_dirname("stock")
        if not enriched_dir.exists():
            logger.warning("enriched data not available for intraday regime")
            return None
        
        # 加载今日数据（带预热）
        load_start = target_date - timedelta(days=150)
        read_cols = ["symbol", "date", "open", "high", "low", "close", "volume", 
                     "amount", "raw_close", "raw_high", "raw_low"]
        
        try:
            lf = scan_enriched_parquet(str(enriched_dir / "**" / "*.parquet")).filter(
                (pl.col("date") >= load_start) & (pl.col("date") <= target_date)
            ).sort(["symbol", "date"])
            available = [c for c in read_cols if c in lf.schema]
            df_hist = lf.select(available).collect()
        except Exception as e:
            logger.warning("intraday regime load failed: %s", e)
            return None
        
        if df_hist.is_empty():
            return None
        
        # 计算指标 - 只计算需要的列，与 regime_builder 保持一致
        df_full = compute_indicators(df_hist, needed={"change_pct", "ma20"})
        
        # 计算涨跌停信号
        instruments = repo.get_instruments()
        if instruments is not None and not instruments.is_empty():
            df_full = compute_limit_signals(
                df_full,
                instruments,
                needed={"signal_limit_up", "signal_limit_down", "signal_broken_limit_up"},
                historical_shares=repo.get_historical_shares(),
            )
        
        # 只保留今日数据
        df_today = df_full.filter(pl.col("date") == target_date)
        if df_today.is_empty():
            logger.warning("no today data for intraday regime")
            return None
        
        # 加载指数数据 - 正确格式: {date: pct}
        index_pct_map = {}
        try:
            df_idx = repo.get_index_daily("000001.SH", target_date, target_date, columns=["date", "change_pct"])
            if not df_idx.is_empty() and "change_pct" in df_idx.columns:
                for r in df_idx.iter_rows(named=True):
                    idx_date = r["date"]
                    pct = float(r.get("change_pct") or 0)  # 注意：这里用小数，不需要 *100
                    index_pct_map[idx_date] = pct
        except Exception as e:
            logger.warning("index pct load failed for intraday regime: %s", e)
        
        # 复用 regime_builder 的聚合逻辑
        # 涨停数统一封板口径(与实时情绪/看板一致): 扣除假涨停后再算封板率与评分
        df_agg = regime_builder._aggregate_daily(
            df_today, index_pct_map,
            fake_up=_depth_fake_limit_up(depth_service, target_date),
        )
        
        if df_agg.is_empty():
            return None
        
        # 获取第一行（今日数据）
        row = df_agg.to_dicts()[0]
        
        # 添加时间信息（支持秒级）
        timestamp = int(now.timestamp() * 1000)
        # 对于特定秒点显示完整时间，其他显示分钟即可
        special_seconds = [10, 20]
        if (now.hour == 9 and now.minute in [15, 25, 30] and now.second in special_seconds) or \
           (now.hour == 15 and now.minute == 0 and now.second in special_seconds):
            time_str = now.strftime("%H:%M:%S")
        else:
            time_str = now.strftime("%H:%M")
        row["timestamp"] = timestamp
        row["time"] = time_str
        
        return row
    
    except Exception as e:
        logger.exception("compute_intraday_regime failed: %s", e)
        return None


class IntradayRegimeService:
    """实时环境计算服务 - 每分钟计算并存储环境综合分及四维度数据。"""
    
    def __init__(self):
        self._repo = None
        self._data_dir = None
        self._depth_service = None
        self._running = False
        self._thread = None
        self._lock = threading.Lock()
        self._last_calculation_time = None

    def set_repo(self, repo):
        self._repo = repo
        if repo:
            self._data_dir = repo.store.data_dir

    def set_depth_service(self, depth_service):
        self._depth_service = depth_service

    def compute_now(self, force: bool = False) -> dict[str, Any] | None:
        """立即计算一次实时环境。"""
        if not self._repo or not self._data_dir:
            return None

        # 检查是否在交易时段（除非强制）
        if not force and not is_trading_time():
            return None

        record = _compute_intraday_regime_impl(self._repo, self._depth_service)
        
        if record:
            append_intraday_regime(self._data_dir, record)
            self._last_calculation_time = cn_now()
            logger.info("intraday regime computed: %s %s", record.get("time"), record.get("state"))
        
        return record
    
    def get_latest(self, target_date: date | None = None) -> dict[str, Any] | None:
        """获取最新的实时环境数据。"""
        df = load_intraday_regime(self._data_dir, target_date)
        if df.is_empty():
            return None
        latest = df.sort("timestamp", descending=True).head(1)
        return latest.to_dicts()[0] if latest.height > 0 else None
    
    def get_history(self, target_date: date | None = None) -> list[dict[str, Any]]:
        """获取当日的历史分钟级环境数据。"""
        df = load_intraday_regime(self._data_dir, target_date)
        if df.is_empty():
            return []
        return df.sort("timestamp").to_dicts()
    
    def _run_loop(self):
        """后台运行循环。"""
        logger.info("intraday regime service started")
        
        while self._running:
            try:
                now = cn_now()
                
                # 使用 should_record_now 判断是否需要记录
                if should_record_now(now):
                    # 检查距离上次计算是否超过5秒（避免重复）
                    if (self._last_calculation_time is None or 
                        (now - self._last_calculation_time).total_seconds() > 5):
                        self.compute_now()
                
                # 每秒检查一次
                for _ in range(10):
                    if not self._running:
                        break
                    threading.Event().wait(0.1)
            
            except Exception as e:
                logger.exception("intraday regime loop error: %s", e)
                threading.Event().wait(5)
    
    def start(self):
        """启动实时环境计算服务。"""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(target=self._run_loop, daemon=True, name="intraday-regime")
            self._thread.start()
            logger.info("intraday regime service starting")
    
    def stop(self):
        """停止实时环境计算服务。"""
        with self._lock:
            if not self._running:
                return
            self._running = False
        
        if self._thread:
            self._thread.join(timeout=5)
            logger.info("intraday regime service stopped")


# 全局单例
_intraday_regime_service: IntradayRegimeService | None = None


def get_intraday_regime_service() -> IntradayRegimeService:
    """获取实时环境服务单例。"""
    global _intraday_regime_service
    if _intraday_regime_service is None:
        _intraday_regime_service = IntradayRegimeService()
    return _intraday_regime_service

