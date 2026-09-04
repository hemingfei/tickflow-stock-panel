"""看板快照落盘与回溯 — 开盘时段每 5 分钟把看板全量数据存为结构化 JSON。

快照内容复用 app/api/overview.py 的 build_board_snapshot (单个 JSON 覆盖
「市场看板」页面全部信息), 与看板定时推送共用同一构建器, 经 main.py 依赖
注入, 避免服务层反向依赖 API 层。

调度设计: 与 board_webhook_push 相同 — 注册每分钟自检的常驻 CronTrigger
job, 每次 fire 时在 Python 侧判断: 工作日 + A 股连续竞价时段 (北京时间,
见 market_time.in_continuous_session) + 对齐 5 分钟刻度 (09:30 / 13:00 起
每 5 分钟, 含开盘与收盘节点)。窗口外的 fire 只是一次廉价的时间判断;
同一分钟去重, misfire 补跑不重复写。

存储布局: {data_dir}/board_snapshots/{YYYY-MM-DD}/{HHMM}.json, 临时文件 +
os.replace 原子写。日期与时刻在写入/读取前都先解析再重新格式化, 目录与
文件名不直接拼接外部输入, 防路径穿越。

回溯读取: load_nearest 取「不晚于目标时刻的最近节点」; 目标早于当日首个
节点时回退首个节点, 保证拖到开盘前仍有内容可看。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections.abc import Callable
from datetime import datetime
from datetime import time as dt_time
from pathlib import Path
from typing import Any

from app.market_time import cn_now, in_continuous_session

logger = logging.getLogger(__name__)

SNAPSHOT_DIR_NAME = "board_snapshots"
RECORD_INTERVAL_MINUTES = 5
# 两个竞价时段起点 (北京时间), 与 market_time 的连续竞价窗口一致
_SESSION_STARTS = (dt_time(9, 30), dt_time(13, 0))

# 快照构建器由 main.py 注入 (api.overview.build_board_snapshot)
_snapshot_builder: Callable[[Any], dict] | None = None


def set_snapshot_builder(builder: Callable[[Any], dict]) -> None:
    global _snapshot_builder
    _snapshot_builder = builder


def snapshots_root(data_dir: Path | str) -> Path:
    return Path(data_dir) / SNAPSHOT_DIR_NAME


# ================================================================
# 落盘刻度判断 (纯函数, 可测)
# ================================================================

def is_aligned_record_tick(now: datetime) -> bool:
    """当前时刻是否为落盘刻度: 工作日 + 连续竞价时段内 + 距时段起点整 5 分钟 (含起点)。

    含起点即 09:30 / 13:00 本身也落盘 (开盘与午后开盘节点), 15:00 收盘节点
    由下午窗口对齐覆盖 (13:00 + 24*5min)。
    """
    if now.weekday() >= 5:
        return False
    if not in_continuous_session(now):
        return False
    minute_of_day = now.hour * 60 + now.minute
    return any(
        minute_of_day >= start.hour * 60 + start.minute
        and (minute_of_day - (start.hour * 60 + start.minute)) % RECORD_INTERVAL_MINUTES == 0
        for start in _SESSION_STARTS
    )


# ================================================================
# 路径与解析 (输入先解析再格式化, 不直接拼外部字符串)
# ================================================================

def normalize_day(day: str) -> str:
    """"2026-9-1" → "2026-09-01"; 非法输入抛 ValueError (fail-closed)。"""
    # strptime 对 %m/%d 接受不补零输入, 与 board_webhook_push.parse_hhmm 的宽松口径一致
    parsed = datetime.strptime(str(day).strip(), "%Y-%m-%d").date()
    return parsed.isoformat()


def normalize_hhmm(hhmm: str) -> str:
    """"9:5" → "09:05"; 非法输入抛 ValueError。"""
    parsed = datetime.strptime(str(hhmm).strip(), "%H:%M").time()
    return parsed.strftime("%H:%M")


def snapshot_path(data_dir: Path | str, day: str, hhmm: str) -> Path:
    return snapshots_root(data_dir) / normalize_day(day) / f"{normalize_hhmm(hhmm).replace(':', '')}.json"


# ================================================================
# 写入
# ================================================================

def record_snapshot(app_state: Any, now: datetime | None = None) -> dict:
    """构建一次看板快照并落盘。返回 {date, time, path, bytes, as_of}。

    目录按北京时间墙钟日期归属 — 回溯语义是「那个时刻页面显示什么」,
    与实时数据源实际归属交易日 (as_of) 解耦, as_of 由快照内容自说明。
    """
    if _snapshot_builder is None:
        raise RuntimeError("快照构建器未初始化 (应用尚未启动完成)")
    now = now or cn_now()
    snapshot = _snapshot_builder(app_state)
    payload = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")

    day = now.strftime("%Y-%m-%d")
    hhmm = now.strftime("%H:%M")
    path = snapshot_path(app_state.repo.store.data_dir, day, hhmm)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, path)
    return {"date": day, "time": hhmm, "path": str(path), "bytes": len(payload),
            "as_of": snapshot.get("as_of") if isinstance(snapshot, dict) else None}


# ================================================================
# 读取 (回溯)
# ================================================================

def list_dates(data_dir: Path | str) -> list[str]:
    """有快照的日期 (升序)。空目录/非目录名/无 JSON 的日期一律跳过。"""
    root = snapshots_root(data_dir)
    if not root.is_dir():
        return []
    out: list[str] = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        try:
            day = normalize_day(entry.name)
        except ValueError:
            continue
        if any(entry.glob("*.json")):
            out.append(day)
    return sorted(out)


def list_times(data_dir: Path | str, day: str) -> list[str]:
    """指定日期的快照时刻列表 ("HH:MM", 升序)。日期非法抛 ValueError。"""
    target = snapshots_root(data_dir) / normalize_day(day)
    if not target.is_dir():
        return []
    times: list[str] = []
    for entry in target.glob("*.json"):
        stem = entry.stem
        # 文件名形如 "0935" (无冒号), 补回冒号后校验
        if len(stem) == 4 and stem.isdigit():
            try:
                times.append(normalize_hhmm(f"{stem[:2]}:{stem[2:]}"))
            except ValueError:
                continue
    return sorted(times)


def load_nearest(data_dir: Path | str, day: str, hhmm: str | None = None) -> tuple[str, dict] | None:
    """取该日期「不晚于 hhmm 的最近节点」; hhmm 缺省/早于首节点时取最后一个/第一个节点。

    Returns:
        (节点时刻 "HH:MM", 快照 dict); 该日期无快照时为 None。
    """
    times = list_times(data_dir, day)
    if not times:
        return None
    if hhmm is None or not str(hhmm).strip():
        chosen = times[-1]
    else:
        target = normalize_hhmm(hhmm)
        at_or_before = [t for t in times if t <= target]
        chosen = at_or_before[-1] if at_or_before else times[0]
    path = snapshots_root(data_dir) / normalize_day(day) / f"{chosen.replace(':', '')}.json"
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("看板快照读取失败 %s: %s", path.name, e)
        return None
    return chosen, snapshot


# ================================================================
# 定时落盘 job (每分钟自检, 与 board_webhook_push 同一模式)
# ================================================================

_record_lock = threading.Lock()
_last_record_minute: str | None = None


def reset_record_state() -> None:
    """内存态复位 (测试用)。"""
    global _last_record_minute
    with _record_lock:
        _last_record_minute = None


def run_due_record(app_state: Any, now: datetime | None = None) -> dict | None:
    """定时 job 的同步执行体: 到刻度才落盘, 未到点/同分钟重复一律跳过。

    构建或写盘失败只记警告, 不向调度器抛异常 (下一次刻度会再试)。

    Returns:
        record_snapshot 的结果 dict; 本轮未触发时为 None。
    """
    now = now or cn_now()
    if not is_aligned_record_tick(now):
        return None

    minute_key = now.strftime("%Y-%m-%d %H:%M")
    with _record_lock:
        global _last_record_minute
        if _last_record_minute == minute_key:
            return None
        _last_record_minute = minute_key

    try:
        result = record_snapshot(app_state, now=now)
    except Exception as e:  # 落盘失败不阻塞调度器
        logger.warning("看板快照落盘失败 (%s %s): %s", now.strftime("%Y-%m-%d"), now.strftime("%H:%M"), e)
        return None
    logger.info(
        "看板快照已落盘 %s %s (%s 字节, as_of=%s)",
        result["date"], result["time"], result["bytes"], result["as_of"],
    )
    return result


async def run_due_record_async(app_state: Any) -> None:
    """APScheduler 协程 job: 重活放线程池, 不阻塞调度器事件循环。"""
    await asyncio.to_thread(run_due_record, app_state)


def register_record_job(scheduler, app_state: Any) -> None:
    """注册每分钟自检的落盘 job (常驻, 无配置项)。"""
    from apscheduler.triggers.cron import CronTrigger

    # 注意: 必须传协程函数对象本身 (配合 args), 不能用 lambda 包裹,
    # 否则 APScheduler 按同步函数处理, 协程不会真正执行 (见 daily_pipeline 同类注释)。
    scheduler.add_job(
        run_due_record_async,
        args=[app_state],
        trigger=CronTrigger(minute="*", timezone="Asia/Shanghai"),
        id="board_snapshot_record",
        misfire_grace_time=30,
        max_instances=1,
        replace_existing=True,
    )
