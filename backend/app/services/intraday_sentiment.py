"""实时情绪计算服务 - 分钟级情绪指标。

交易日实时行情期间，每分钟获取一次完整的情绪指标六维数据，保存记录。
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import polars as pl

from app.services import sentiment_builder
from app.services.market_overview_builder import _score, _dimension_rank, _finite, CORE_INDEX_SYMBOLS
from app.market_time import cn_now, cn_today, trading_minutes_elapsed
from app.tickflow.repository import enriched_dirname
from app.indicators.pipeline import compute_indicators, compute_limit_signals, compute_signals
from app.parquet import scan_enriched_parquet

logger = logging.getLogger(__name__)

# 数据存储目录
INTRADAY_SENTIMENT_DIR = "sentiment_intraday"

# A 股交易时段检查
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
    """判断当前时刻是否应该记录情绪数据。"""
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


def intraday_sentiment_path(data_dir: Path, target_date: date | None = None) -> Path:
    """获取指定日期的分钟级情绪数据存储路径。"""
    target_date = target_date or cn_today()
    date_str = target_date.strftime("%Y-%m-%d")
    return data_dir / INTRADAY_SENTIMENT_DIR / f"{date_str}.parquet"


def load_intraday_sentiment(data_dir: Path, target_date: date | None = None) -> pl.DataFrame:
    """加载指定日期的分钟级情绪数据。"""
    path = intraday_sentiment_path(data_dir, target_date)
    if not path.exists():
        return pl.DataFrame()
    try:
        return pl.read_parquet(path)
    except Exception as e:
        logger.warning("load_intraday_sentiment failed: %s", e)
        return pl.DataFrame()


def save_intraday_sentiment(data_dir: Path, df: pl.DataFrame, target_date: date | None = None) -> None:
    """保存分钟级情绪数据。"""
    if df.is_empty():
        return
    path = intraday_sentiment_path(data_dir, target_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(path)


def append_intraday_sentiment(data_dir: Path, record: dict[str, Any], target_date: date | None = None) -> None:
    """追加单条分钟级情绪记录。"""
    existing = load_intraday_sentiment(data_dir, target_date)
    new_record = pl.DataFrame([record])
    
    if existing.is_empty():
        combined = new_record
    else:
        # 移除已存在的相同时间戳记录
        existing = existing.filter(pl.col("timestamp") != record["timestamp"])
        combined = pl.concat([existing, new_record], how="vertical_relaxed")
    
    combined = combined.sort("timestamp")
    save_intraday_sentiment(data_dir, combined, target_date)


def _compute_intraday_sentiment_impl(repo, depth_service=None) -> dict[str, Any] | None:
    """计算当前时刻的实时情绪指标。"""
    try:
        now = cn_now()
        target_date = now.date()
        
        # 加载完整的 enriched 数据
        enriched_dir = repo.store.data_dir / enriched_dirname("stock")
        if not enriched_dir.exists():
            logger.warning("enriched data not available for intraday sentiment")
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
            logger.warning("intraday sentiment load failed: %s", e)
            return None
        
        if df_hist.is_empty():
            return None
        
        # 计算指标
        df_full = compute_indicators(df_hist)
        df_full = compute_signals(df_full)
        
        # 计算涨跌停信号
        instruments = repo.get_instruments()
        if instruments is not None and not instruments.is_empty():
            df_full = compute_limit_signals(
                df_full,
                instruments,
                historical_shares=repo.get_historical_shares(),
            )
            
            # JOIN instruments
            if "name" not in df_full.columns:
                inst_cols = [c for c in ["symbol", "name", "total_shares", "float_shares"] if c in instruments.columns]
                df_full = df_full.join(instruments.select(inst_cols), on="symbol", how="left")
        
        # 只保留今日数据
        df_today = df_full.filter(pl.col("date") == target_date)
        if df_today.is_empty():
            logger.warning("no today data for intraday sentiment")
            return None
        
        # 加载指数数据
        index_pct_map = {}
        try:
            for symbol in CORE_INDEX_SYMBOLS:
                df_idx = repo.get_index_daily(symbol, target_date, target_date, columns=["date", "change_pct"])
                if not df_idx.is_empty() and "change_pct" in df_idx.columns:
                    for r in df_idx.iter_rows(named=True):
                        idx_date = r["date"]
                        pct = float(r.get("change_pct") or 0) * 100
                        if idx_date not in index_pct_map:
                            index_pct_map[idx_date] = {}
                        index_pct_map[idx_date][symbol] = pct
        except Exception as e:
            logger.warning("index pct load failed for intraday: %s", e)
        
        # 复用 sentiment_builder 的单日聚合逻辑
        record = sentiment_builder._aggregate_single_day(
            repo, target_date, df_today, index_pct_map, depth_service
        )
        
        if record:
            # 添加时间信息（支持秒级）
            timestamp = int(now.timestamp() * 1000)
            # 对于特定秒点显示完整时间，其他显示分钟即可
            special_seconds = [10, 20]
            if (now.hour == 9 and now.minute in [15, 25, 30] and now.second in special_seconds) or \
               (now.hour == 15 and now.minute == 0 and now.second in special_seconds):
                time_str = now.strftime("%H:%M:%S")
            else:
                time_str = now.strftime("%H:%M")
            record["timestamp"] = timestamp
            record["time"] = time_str
        
        return record
    
    except Exception as e:
        logger.exception("compute_intraday_sentiment failed: %s", e)
        return None


class IntradaySentimentService:
    """实时情绪计算服务 - 每分钟计算并存储情绪指标。"""
    
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
        """立即计算一次实时情绪。"""
        if not self._repo or not self._data_dir:
            return None
        
        # 检查是否在交易时段（除非强制）
        if not force and not is_trading_time():
            return None
        
        record = _compute_intraday_sentiment_impl(self._repo, self._depth_service)
        
        if record:
            append_intraday_sentiment(self._data_dir, record)
            self._last_calculation_time = cn_now()
            logger.info("intraday sentiment computed: %s %s", record.get("time"), record.get("emotion_label"))
        
        return record
    
    def get_latest(self, target_date: date | None = None) -> dict[str, Any] | None:
        """获取最新的实时情绪数据。"""
        df = load_intraday_sentiment(self._data_dir, target_date)
        if df.is_empty():
            return None
        latest = df.sort("timestamp", descending=True).head(1)
        return latest.to_dicts()[0] if latest.height > 0 else None
    
    def get_history(self, target_date: date | None = None) -> list[dict[str, Any]]:
        """获取当日的历史分钟级情绪数据。"""
        df = load_intraday_sentiment(self._data_dir, target_date)
        if df.is_empty():
            return []
        return df.sort("timestamp").to_dicts()
    
    def _run_loop(self):
        """后台运行循环。"""
        logger.info("intraday sentiment service started")
        
        while self._running:
            try:
                now = cn_now()
                
                # 使用新的 should_record_now 判断是否需要记录
                if should_record_now(now):
                    # 检查距离上次计算是否超过5秒（避免重复，因为现在支持秒级记录）
                    if (self._last_calculation_time is None or 
                        (now - self._last_calculation_time).total_seconds() > 5):
                        self.compute_now()
                
                # 每秒检查一次
                for _ in range(10):
                    if not self._running:
                        break
                    threading.Event().wait(0.1)
            
            except Exception as e:
                logger.exception("intraday sentiment loop error: %s", e)
                threading.Event().wait(5)
    
    def start(self):
        """启动实时情绪计算服务。"""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(target=self._run_loop, daemon=True, name="intraday-sentiment")
            self._thread.start()
            logger.info("intraday sentiment service starting")
    
    def stop(self):
        """停止实时情绪计算服务。"""
        with self._lock:
            if not self._running:
                return
            self._running = False
        
        if self._thread:
            self._thread.join(timeout=5)
            logger.info("intraday sentiment service stopped")


# 全局单例
_intraday_sentiment_service: IntradaySentimentService | None = None


def get_intraday_sentiment_service() -> IntradaySentimentService:
    """获取实时情绪服务单例。"""
    global _intraday_sentiment_service
    if _intraday_sentiment_service is None:
        _intraday_sentiment_service = IntradaySentimentService()
    return _intraday_sentiment_service
