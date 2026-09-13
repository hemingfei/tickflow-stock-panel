"""指数共振 —— 指数上行时在自选分组(板块)中找最强板块与最强个股。

三级共振: 指数向上 -> 最强板块分组 -> 板块内最强个股。

- 监测配置持久化于 ``data/user_data/resonance_monitors.json``, 支持多个监测,
  每个监测选择核心四只指数之一 (index_const 单一权威)、生效时间段、
  动量窗口与阈值、参与比较的自选分组范围。
- 窗口涨幅口径与 sector_monitor 一致: 基于实时快照 change_pct 的窗口差值,
  窗口起点允许 90s 容差; 历史仅在监测时段内积累, 跨日清零。
- 数据边界单位: 股票 enriched change_pct 为小数制 (0.0366 = 3.66%);
  指数快照 change_pct 为百分数 (3.66 = 3.66%), 入史前统一 /100 为小数。
  API 输出统一为百分数 (3.66), 前端直接 toFixed(2)% 展示。
- 由 quote_service._evaluate_monitors 在连续竞价时段内驱动; 无启用的监测
  时不做任何计算。
"""
from __future__ import annotations

import copy
import json
import logging
import math
import os
import threading
import time
import uuid
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from app.market_time import cn_now, cn_today, trading_minutes_elapsed_from_dt
from app.services import watchlist_group_store
from app.services.index_const import CORE_INDEX_NAMES, CORE_INDEX_SYMBOLS

logger = logging.getLogger(__name__)

_STORE_LOCK = threading.RLock()
_VERSION = 1
_WINDOW_TOLERANCE_SECONDS = 90
_HISTORY_MARGIN_SECONDS = 120
_NAME_MAP_TTL_SECONDS = 60.0

# 状态枚举: disabled(监测已停用) / off_window(不在监测时段) / no_data(指数快照未就绪)
#           / warming(窗口历史不足) / up(向上) / down(向下) / flat(震荡)


def _store_path() -> Path:
    from app.config import settings

    p = settings.data_dir / "user_data" / "resonance_monitors.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_hhmm(value: Any) -> int | None:
    """'HH:MM' -> 当日分钟数; 已是分钟数(0..1439)直接返回 (保证幂等); 非法返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= 24 * 60 - 1 else None
    if isinstance(value, float):
        value = int(value)
        return value if 0 <= value <= 24 * 60 - 1 else None
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


# 可推送的 webhook 渠道 (与监控规则 webhook_channels 同一集合)
NOTIFY_CHANNELS = {"feishu", "wecom", "kol", "custom", "email"}


def normalize_monitor(raw: object) -> dict | None:
    """清洗单条监测配置; 无效(缺 id/name/指数非法)返回 None。"""
    if not isinstance(raw, dict):
        return None
    monitor_id = str(raw.get("id") or "").strip()
    name = str(raw.get("name") or "").strip()
    index_symbol = str(raw.get("index_symbol") or "").strip().upper()
    if not monitor_id or not name or index_symbol not in CORE_INDEX_SYMBOLS:
        return None

    ranges: list[dict] = []
    raw_ranges = raw.get("time_ranges")
    if isinstance(raw_ranges, list):
        for item in raw_ranges:
            if not isinstance(item, dict):
                continue
            start = _parse_hhmm(item.get("start"))
            end = _parse_hhmm(item.get("end"))
            if start is None or end is None or start >= end:
                continue
            if any(r["start"] == start and r["end"] == end for r in ranges):
                continue
            ranges.append({"start": start, "end": end})
    ranges.sort(key=lambda r: (r["start"], r["end"]))

    group_ids: list[str] = []
    raw_groups = raw.get("group_ids")
    if isinstance(raw_groups, list):
        seen: set[str] = set()
        for gid in raw_groups:
            gid = str(gid or "").strip()
            if gid and gid not in seen:
                seen.add(gid)
                group_ids.append(gid)

    webhook_channels = [
        ch for ch in (raw.get("webhook_channels") or [])
        if isinstance(ch, str) and ch.strip().lower() in NOTIFY_CHANNELS
    ]
    webhook_channels = [ch.strip().lower() for ch in webhook_channels]

    notify_cooldown = raw.get("notify_cooldown_seconds")
    try:
        notify_cooldown = int(notify_cooldown)
    except (TypeError, ValueError):
        notify_cooldown = 600
    notify_cooldown = max(0, min(86400, notify_cooldown))

    def _bounded_float(key: str, default: float, low: float, high: float) -> float:
        value = _finite(raw.get(key))
        if value is None or value < low or value > high:
            return default
        return value

    window_s = raw.get("window_seconds")
    if window_s is None:
        # 旧配置迁移: window_minutes (分钟) -> 秒
        try:
            window_s = float(raw.get("window_minutes")) * 60
        except (TypeError, ValueError):
            window_s = 300
    try:
        window_s = round(float(window_s))
    except (TypeError, ValueError):
        window_s = 300
    if not (30 <= window_s <= 3600):
        window_s = 300

    min_members = raw.get("min_group_members")
    try:
        min_members = int(min_members)
    except (TypeError, ValueError):
        min_members = 3
    if not (2 <= min_members <= 500):
        min_members = 3

    return {
        "id": monitor_id,
        "name": name,
        "index_symbol": index_symbol,
        "enabled": bool(raw.get("enabled", True)),
        "time_ranges": ranges,
        "window_seconds": window_s,
        "index_threshold_pct": _bounded_float("index_threshold_pct", 0.3, 0.01, 10.0),
        "group_up_ratio": _bounded_float("group_up_ratio", 0.6, 0.0, 1.0),
        "min_group_members": min_members,
        "group_ids": group_ids,
        # 三维判定附加阈值 (旧配置缺字段时取默认):
        # 涨幅门槛 <=0 表示停用; 量能倍数 <=0 表示停用; 量比 = 窗口每分钟量 / 当日每分钟平均量
        "index_change_pct_gate": _bounded_float("index_change_pct_gate", 0.2, -1.0, 20.0),
        "index_volume_ratio_gate": _bounded_float("index_volume_ratio_gate", 1.5, 0.0, 20.0),
        "group_change_pct_gate": _bounded_float("group_change_pct_gate", 0.0, -1.0, 20.0),
        "group_volume_ratio_gate": _bounded_float("group_volume_ratio_gate", 1.3, 0.0, 20.0),
        "leader_change_pct_gate": _bounded_float("leader_change_pct_gate", 0.5, -1.0, 20.0),
        "leader_volume_ratio_gate": _bounded_float("leader_volume_ratio_gate", 1.5, 0.0, 20.0),
        "webhook_channels": webhook_channels,
        "notify_cooldown_seconds": notify_cooldown,
        "created_at": str(raw.get("created_at") or ""),
    }


def _normalize(data: object) -> dict:
    out = {"version": _VERSION, "monitors": []}
    if not isinstance(data, dict):
        return out
    monitors = data.get("monitors")
    if isinstance(monitors, list):
        seen: set[str] = set()
        for raw in monitors:
            monitor = normalize_monitor(raw)
            if monitor and monitor["id"] not in seen:
                seen.add(monitor["id"])
                out["monitors"].append(monitor)
    out["monitors"].sort(key=lambda m: (m["created_at"], m["id"]))
    return out


def _write_disk(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


_store_cache: dict | None = None
_store_sig: tuple[int, int] | None = None


def _load_store() -> dict:
    """读取监测配置 (mtime 签名缓存)。文件损坏时备份后按空配置继续。"""
    global _store_cache, _store_sig
    with _STORE_LOCK:
        p = _store_path()
        if not p.exists():
            data = _normalize(None)
            _write_disk(p, data)
            _store_cache = data
            _store_sig = (p.stat().st_mtime_ns, p.stat().st_size)
            return copy.deepcopy(data)
        try:
            sig = (p.stat().st_mtime_ns, p.stat().st_size)
        except OSError:
            return copy.deepcopy(_store_cache) if _store_cache is not None else _normalize(None)
        if _store_cache is not None and sig == _store_sig:
            return copy.deepcopy(_store_cache)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("resonance monitor store malformed: %s", e)
            try:
                bak = p.parent / f"resonance_monitors.json.bak.{int(time.time())}"
                p.rename(bak)
                logger.info("backed up corrupted resonance store to %s", bak)
            except OSError:
                pass
            data = None
        data = _normalize(data)
        _store_cache = data
        _store_sig = sig
        return copy.deepcopy(data)


def _save_store(data: dict) -> dict:
    global _store_cache, _store_sig
    with _STORE_LOCK:
        clean = _normalize(data)
        p = _store_path()
        _write_disk(p, clean)
        _store_cache = clean
        _store_sig = (p.stat().st_mtime_ns, p.stat().st_size)
        return copy.deepcopy(clean)


def _new_monitor_id() -> str:
    return f"res_{uuid.uuid4().hex[:12]}"


def in_time_ranges(now_cn: datetime, ranges: list[dict]) -> bool:
    """当前北京时间是否落在任一监测时段内; 空时段 = 整个连续竞价时段。"""
    if not ranges:
        return True
    minute_of_day = now_cn.hour * 60 + now_cn.minute
    return any(r["start"] <= minute_of_day < r["end"] for r in ranges)


def _window_label(seconds: int) -> str:
    """窗口时长展示标签: 30 -> '30秒', 300 -> '5分钟'。"""
    if seconds < 60 or seconds % 60 != 0:
        return f"{seconds}秒"
    return f"{seconds // 60}分钟"


def _window_point(
    history: deque[tuple[float, float, float | None]],
    now: float,
    window_seconds: int,
) -> tuple[float, float, float | None] | None:
    """窗口起点参考点 (ts, change_pct, volume); 容差 90s; 历史未覆盖整个窗口返回 None。"""
    if not history:
        return None
    cutoff = now - window_seconds
    for item in reversed(history):
        if item[0] <= cutoff:
            if item[0] < cutoff - _WINDOW_TOLERANCE_SECONDS:
                return None
            return item
    return None


def window_change(
    history: deque[tuple[float, float, float | None]],
    now: float,
    window_seconds: int,
) -> float | None:
    """窗口起点涨跌幅参考值 (与 sector_monitor._window_change 同口径)。"""
    point = _window_point(history, now, window_seconds)
    return None if point is None else point[1]


def volume_ratio(
    now_vol: float | None,
    point: tuple[float, float, float | None] | None,
    window_seconds: int,
    now: float,
    elapsed_now: float,
) -> float | None:
    """窗口量比 = 窗口每分钟成交量 / 窗口起点时刻的当日每分钟平均量。

    基准取窗口起点时刻的日均 (而非当前日均), 避免开盘初期"窗口量≈全天量"
    导致量比恒为 1 的自比失真。连续竞价内交易分钟差等于真实分钟差;
    跨午休的窗口会被 warming 挡住, 不会进入此计算。
    数据缺失/跨日重置(窗口量为负)/开盘基准不足 0.5 分钟 -> None (维度不可判定)。
    """
    if now_vol is None or point is None:
        return None
    base_ts, _, base_vol = point
    if base_vol is None:
        return None
    elapsed_base = elapsed_now - (now - base_ts) / 60
    if elapsed_base < 0.5:
        return None
    window_vol = now_vol - base_vol
    if window_vol <= 0:
        return None
    per_min_day = base_vol / elapsed_base
    if per_min_day <= 0:
        return None
    return (window_vol / (window_seconds / 60)) / per_min_day


def _fmt_pct(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f}%"


def _build_event(
    monitor: dict,
    index_state: dict,
    top: dict,
    leader: dict,
    now: float,
) -> dict:
    """共振上升沿 -> 通知事件 (与监控规则 rule_events 同构, source=resonance)。

    文案面向用户: 中文板块名/个股名, 不泄漏内部枚举; 一次共振episode只发
    上升沿一条, 由 notify_cooldown_seconds 进一步防抖。
    """
    index_name = index_state.get("name") or monitor["index_symbol"]
    window_s = int(monitor["window_seconds"])
    message = (
        f"{index_name} 向上: {_window_label(window_s)} {_fmt_pct(index_state.get('window_change_pct'))}"
        f" · 现涨 {_fmt_pct(index_state.get('change_pct'))}"
        f" · 量比 {index_state.get('volume_ratio') if index_state.get('volume_ratio') is not None else '—'}"
        f"; 最强板块「{top['name']}」"
        f" {_fmt_pct(top.get('avg_window_pct'))}"
        f" (上涨 {top.get('up_count', 0)}/{top.get('valid_count', 0)})"
    )
    if leader:
        leader_name = leader.get("name") or leader.get("symbol") or ""
        message += (
            f"; 龙头 {leader_name}({leader.get('symbol')})"
            f" {_fmt_pct(leader.get('window_change_pct'))}"
            f" 量比 {leader.get('volume_ratio') if leader.get('volume_ratio') is not None else '—'}"
        )
    return {
        "ts": int(now * 1000),
        "rule_id": monitor["id"],
        "rule_name": monitor["name"],
        "strategy_id": None,
        "source": "resonance",
        "type": "resonance_up",
        "symbol": monitor["index_symbol"],
        "name": monitor["name"],
        "message": message,
        "price": index_state.get("price"),
        "change_pct": index_state.get("change_pct"),
        "signals": [],
        "severity": "info",
        "conditions": [],
        "logic": "and",
        "webhook_channels": list(monitor.get("webhook_channels") or []),
        "resonance": {
            "monitor_id": monitor["id"],
            "monitor_name": monitor["name"],
            "index": {
                "symbol": index_state.get("symbol"),
                "name": index_name,
                "change_pct": index_state.get("change_pct"),
                "window_change_pct": index_state.get("window_change_pct"),
                "volume_ratio": index_state.get("volume_ratio"),
            },
            "group": {
                "group_id": top["group_id"],
                "name": top["name"],
                "avg_window_pct": top.get("avg_window_pct"),
                "up_count": top.get("up_count"),
                "valid_count": top.get("valid_count"),
            },
            "leader": dict(leader) if leader else None,
        },
    }


def _pick_leader(
    members: list[dict], change_gate: float, volume_gate: float,
) -> dict | None:
    """共振龙头 = 通过涨幅/量能门禁的成员中窗口涨幅最强 (并列取当前涨幅, 再取成员序)。

    change_gate < 0 / volume_gate <= 0 表示维度停用; 成员量比不可判定 (None) 不否决。
    """
    candidates = [
        m for m in members
        if m.get("window_change_pct") is not None
        and not (change_gate >= 0 and m["change_pct"] <= change_gate)
        and not (
            volume_gate > 0
            and m.get("volume_ratio") is not None
            and m["volume_ratio"] < volume_gate
        )
    ]
    if not candidates:
        return None
    leader = max(candidates, key=lambda m: (m["window_change_pct"], m["change_pct"]))
    return {
        "symbol": leader["symbol"],
        "name": leader["name"],
        "change_pct": leader["change_pct"],
        "window_change_pct": leader["window_change_pct"],
        "volume_ratio": leader.get("volume_ratio"),
    }


def _scoped_symbols(monitors: list[dict], store_data: dict) -> set[str]:
    """所有启用监测涉及的自选分组成员并集 (只对这些标的维护历史)。

    任一监测 group_ids 为空 = 覆盖全部分组, 成员全部纳入;
    否则只纳入被某监测显式勾选分组的成员。
    """
    if any(not m.get("group_ids") for m in monitors):
        return {str(m["symbol"]).upper() for m in store_data["members"] if m["symbol"]}
    wanted = {gid for m in monitors for gid in (m.get("group_ids") or [])}
    return {
        str(member["symbol"]).upper()
        for member in store_data["members"]
        if member["symbol"] and member["group_id"] in wanted
    }


class IndexResonanceService:
    """维护指数/成员个股的窗口涨幅历史, 并按监测配置计算共振状态。"""

    def __init__(self, repo) -> None:
        self._repo = repo
        self._lock = threading.RLock()
        self._history_day: str | None = None
        self._index_hist: dict[str, deque[tuple[float, float]]] = {}
        self._stock_hist: dict[str, dict[str, deque[tuple[float, float]]]] = {}
        self._resonant_since: dict[str, float] = {}
        self._last_notified: dict[str, float] = {}
        self._pending_events: list[dict] = []
        self._state: dict = {"ts": 0.0, "monitors": {}}
        self._name_map: dict[str, str] = {}
        self._name_map_ts: float = 0.0

    # ── 配置 CRUD ────────────────────────────────────────────────

    def list_monitors(self) -> list[dict]:
        return _load_store()["monitors"]

    def create_monitor(self, payload: dict) -> list[dict]:
        payload = {**payload, "id": str(payload.get("id") or "").strip() or _new_monitor_id()}
        monitor = normalize_monitor(payload)
        if monitor is None:
            raise ValueError("监测配置无效: 需要名称与合法的核心指数代码")
        if not monitor["created_at"]:
            monitor["created_at"] = _now_iso()
        data = _load_store()
        data["monitors"].append(monitor)
        return _save_store(data)["monitors"]

    def update_monitor(self, monitor_id: str, payload: dict) -> list[dict]:
        data = _load_store()
        current = next((m for m in data["monitors"] if m["id"] == monitor_id), None)
        if current is None:
            raise KeyError(monitor_id)
        merged = {**current, **payload, "id": monitor_id}
        monitor = normalize_monitor(merged)
        if monitor is None:
            raise ValueError("监测配置无效: 需要名称与合法的核心指数代码")
        data["monitors"] = [monitor if m["id"] == monitor_id else m for m in data["monitors"]]
        with self._lock:
            self._drop_history(monitor_id)
        return _save_store(data)["monitors"]

    def delete_monitor(self, monitor_id: str) -> list[dict]:
        data = _load_store()
        if not any(m["id"] == monitor_id for m in data["monitors"]):
            raise KeyError(monitor_id)
        data["monitors"] = [m for m in data["monitors"] if m["id"] != monitor_id]
        with self._lock:
            self._drop_history(monitor_id)
            self._state["monitors"].pop(monitor_id, None)
            self._resonant_since.pop(monitor_id, None)
            self._last_notified.pop(monitor_id, None)
        return _save_store(data)["monitors"]

    def has_enabled_monitors(self) -> bool:
        return any(m.get("enabled") for m in _load_store()["monitors"])

    def consume_events(self) -> list[dict]:
        """取出并清空待通知的共振事件 (由 quote_service 转发到通知管线)。"""
        with self._lock:
            events = self._pending_events
            self._pending_events = []
            return events

    # ── 实时计算 ─────────────────────────────────────────────────

    def update(self, stock_df: pl.DataFrame, index_df: pl.DataFrame, *, now: float) -> None:
        """行情轮询驱动: 刷新各启用监测的共振状态 (仅连续竞价时段被调用)。"""
        monitors = [m for m in _load_store()["monitors"] if m.get("enabled")]
        states: dict[str, dict] = {}
        if monitors:
            self._reset_history_for_day()
            now_cn = cn_now()
            store_data = watchlist_group_store.load()
            index_rows = self._index_rows(index_df)
            scoped_symbols = _scoped_symbols(monitors, store_data)
            stock_rows = self._stock_rows(stock_df, scoped_symbols)

            for monitor in monitors:
                try:
                    states[monitor["id"]] = self._evaluate_monitor(
                        monitor, index_rows, stock_rows, store_data,
                        now=now, now_cn=now_cn,
                    )
                except Exception as e:
                    logger.warning("指数共振监测评估失败 %s: %s", monitor.get("id"), e)

        with self._lock:
            self._state = {"ts": now, "monitors": states}

    def get_state(self) -> dict:
        """配置 + 最新计算状态合并输出 (含停用/时段外监测的静态状态)。"""
        with self._lock:
            live = dict(self._state.get("monitors") or {})
            ts = self._state.get("ts") or 0.0
        monitors = []
        for monitor in _load_store()["monitors"]:
            state = live.get(monitor["id"])
            if state is None:
                state = self._idle_state(monitor)
            monitors.append({"config": monitor, "state": state})
        return {"ts": ts, "monitors": monitors}

    # ── 内部实现 ─────────────────────────────────────────────────

    def _idle_state(self, monitor: dict) -> dict:
        status = "off_window" if monitor.get("enabled") else "disabled"
        return {
            "status": status,
            "in_window": False,
            "resonant": False,
            "resonant_since": None,
            "index": {
                "symbol": monitor["index_symbol"],
                "name": CORE_INDEX_NAMES.get(monitor["index_symbol"], monitor["index_symbol"]),
                "price": None,
                "change_pct": None,
                "window_change_pct": None,
                "volume_ratio": None,
                "gates": {"momentum": None, "change": None, "volume": None},
            },
            "groups": [],
            "top_group_id": None,
            "leader": None,
        }

    def _reset_history_for_day(self) -> None:
        day = cn_today().isoformat()
        with self._lock:
            if self._history_day == day:
                return
            self._history_day = day
            self._index_hist.clear()
            self._stock_hist.clear()
            self._resonant_since.clear()
            self._last_notified.clear()

    def _drop_history(self, monitor_id: str) -> None:
        with self._lock:
            self._index_hist.pop(monitor_id, None)
            self._stock_hist.pop(monitor_id, None)

    @staticmethod
    def _stock_rows(stock_df: pl.DataFrame, scoped: set[str]) -> dict[str, dict]:
        """快照 -> {symbol: {change_pct(小数), volume(累计量), name?}}; 只保留监测范围内的标的。"""
        if stock_df.is_empty() or "symbol" not in stock_df.columns or not scoped:
            return {}
        if "change_pct" in stock_df.columns:
            df = stock_df.select(
                [c for c in ("symbol", "change_pct", "volume", "name") if c in stock_df.columns]
            ).filter(pl.col("symbol").is_in(list(scoped)))
        else:
            return {}
        rows: dict[str, dict] = {}
        for row in df.iter_rows(named=True):
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            rows[symbol] = {
                "change_pct": _finite(row.get("change_pct")),
                "volume": _finite(row.get("volume")),
                "name": str(row.get("name") or "") or None,
            }
        return rows

    @staticmethod
    def _index_rows(index_df: pl.DataFrame) -> dict[str, dict]:
        """指数快照 -> {symbol: {change_pct(小数, 已/100), volume, price, name}}。"""
        if index_df.is_empty() or "symbol" not in index_df.columns:
            return {}
        rows: dict[str, dict] = {}
        for row in index_df.iter_rows(named=True):
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            change_pct = _finite(row.get("change_pct"))
            if change_pct is not None:
                change_pct /= 100  # 指数快照为百分数口径 -> 统一小数
            rows[symbol] = {
                "change_pct": change_pct,
                "volume": _finite(row.get("volume")),
                "price": _finite(row.get("close") or row.get("last_price")),
                "name": str(row.get("name") or "") or None,
            }
        return rows

    def _evaluate_monitor(
        self,
        monitor: dict,
        index_rows: dict[str, dict],
        stock_rows: dict[str, dict],
        store_data: dict,
        *,
        now: float,
        now_cn: datetime,
    ) -> dict:
        window_seconds = int(monitor["window_seconds"])
        threshold = float(monitor["index_threshold_pct"]) / 100  # 配置为百分数 -> 小数
        change_gate = float(monitor["index_change_pct_gate"])  # <0 停用
        volume_gate = float(monitor["index_volume_ratio_gate"])  # <=0 停用
        in_window = in_time_ranges(now_cn, monitor["time_ranges"])

        index_state = {
            "symbol": monitor["index_symbol"],
            "name": CORE_INDEX_NAMES.get(monitor["index_symbol"], monitor["index_symbol"]),
            "price": None,
            "change_pct": None,
            "window_change_pct": None,
            "volume_ratio": None,
            "gates": {"momentum": None, "change": None, "volume": None},
        }
        if not in_window:
            return {
                "status": "off_window", "in_window": False, "resonant": False,
                "resonant_since": None, "index": index_state,
                "groups": [], "top_group_id": None, "leader": None,
            }

        index_row = index_rows.get(monitor["index_symbol"])
        if index_row is None or index_row.get("change_pct") is None:
            return {
                "status": "no_data", "in_window": True, "resonant": False,
                "resonant_since": None, "index": index_state,
                "groups": [], "top_group_id": None, "leader": None,
            }

        index_hist = self._index_hist.setdefault(monitor["id"], deque())
        change = float(index_row["change_pct"])
        index_vol = index_row.get("volume")
        index_hist.append((now, change, index_vol))
        self._prune(index_hist, now, window_seconds)
        index_state.update({
            "price": index_row.get("price"),
            "change_pct": change * 100,
            "name": index_row.get("name") or index_state["name"],
        })

        # 指数三维判定: 动量(窗口涨幅) + 涨幅(当日) + 量能(窗口量比)
        # None = 维度停用或数据不可判定 (不否决); False = 明确未过
        momentum_pass: bool | None = None
        change_pass: bool | None = None
        volume_pass: bool | None = None
        status = "warming"
        base = _window_point(index_hist, now, window_seconds)
        if base is not None:
            index_window = change - base[1]
            index_state["window_change_pct"] = index_window * 100
            momentum_pass = index_window >= threshold
            if change_gate >= 0:
                change_pass = change * 100 > change_gate
            ratio = volume_ratio(index_vol, base, window_seconds, now, trading_minutes_elapsed_from_dt(now_cn))
            index_state["volume_ratio"] = ratio
            if volume_gate > 0 and ratio is not None:
                volume_pass = ratio >= volume_gate
            status = (
                "up"
                if momentum_pass and change_pass is not False and volume_pass is not False
                else ("down" if index_window <= -threshold else "flat")
            )
        index_state["gates"] = {"momentum": momentum_pass, "change": change_pass, "volume": volume_pass}

        groups, members_by_gid = (
            self._evaluate_groups(
                monitor, store_data, stock_rows, now=now, window_seconds=window_seconds, now_cn=now_cn,
            ) if status != "no_data" else ([], {})
        )

        top = next(
            (g for g in groups if g["qualifying"] and g["avg_window_pct"] is not None),
            None,
        )
        leader = None
        resonant = False
        if status == "up" and top is not None:
            leader = _pick_leader(
                members_by_gid.get(top["group_id"], []),
                float(monitor["leader_change_pct_gate"]),
                float(monitor["leader_volume_ratio_gate"]),
            )
            resonant = leader is not None

        resonant_since: float | None = None
        event: dict | None = None
        with self._lock:
            if resonant:
                previous_since = self._resonant_since.get(monitor["id"])
                resonant_since = previous_since if previous_since is not None else now
                self._resonant_since[monitor["id"]] = resonant_since
                # 上升沿 (非共振 -> 共振) 才产生通知; 冷却时间防指数反复翻飞刷屏
                if previous_since is None:
                    cooldown = int(monitor.get("notify_cooldown_seconds") or 0)
                    last = self._last_notified.get(monitor["id"])
                    if last is None or now - last >= cooldown:
                        self._last_notified[monitor["id"]] = now
                        event = _build_event(monitor, index_state, top, leader, now)
            else:
                self._resonant_since.pop(monitor["id"], None)
        if event is not None:
            with self._lock:
                self._pending_events.append(event)

        return {
            "status": status,
            "in_window": True,
            "resonant": resonant,
            "resonant_since": resonant_since,
            "index": index_state,
            "groups": groups,
            "top_group_id": top["group_id"] if top else None,
            "leader": leader,
        }

    def _evaluate_groups(
        self, monitor: dict, store_data: dict, stock_rows: dict[str, dict],
        *, now: float, window_seconds: int, now_cn: datetime,
    ) -> tuple[list[dict], dict[str, list[dict]]]:
        """计算各分组三维快照 (动量/涨幅/量能), 按平均窗口涨幅降序。

        返回 (排行行, 组内成员明细); 成员明细仅供龙头门禁挑选, 不进入状态输出。
        """
        wanted_ids = set(monitor.get("group_ids") or [])
        groups = [
            g for g in store_data["groups"]
            if not wanted_ids or g["id"] in wanted_ids
        ]
        members_by_group: dict[str, list[str]] = {}
        for member in store_data["members"]:
            gid = member["group_id"]
            if wanted_ids and gid not in wanted_ids:
                continue
            members_by_group.setdefault(gid, []).append(str(member["symbol"]).upper())

        monitor_hist = self._stock_hist.setdefault(monitor["id"], {})
        elapsed_now = trading_minutes_elapsed_from_dt(now_cn)
        group_change_gate = float(monitor["group_change_pct_gate"])  # <0 停用
        group_volume_gate = float(monitor["group_volume_ratio_gate"])  # <=0 停用

        rows: list[dict] = []
        members_by_gid: dict[str, list[dict]] = {}
        for group in groups:
            symbols = members_by_group.get(group["id"], [])
            if not symbols:
                continue
            member_rows: list[dict] = []
            for symbol in symbols:
                snapshot = stock_rows.get(symbol)
                if snapshot is None or snapshot.get("change_pct") is None:
                    continue  # 停牌/快照未覆盖: 无有效数据, 不计入
                change = float(snapshot["change_pct"])
                vol = snapshot.get("volume")
                hist = monitor_hist.setdefault(symbol, deque())
                hist.append((now, change, vol))
                self._prune(hist, now, window_seconds)
                base = _window_point(hist, now, window_seconds)
                member_rows.append({
                    "symbol": symbol,
                    "name": snapshot.get("name") or self._resolve_name(symbol),
                    "change_pct": change * 100,
                    "volume_ratio": volume_ratio(vol, base, window_seconds, now, elapsed_now),
                    "window_change_pct": (change - base[1]) * 100 if base is not None else None,
                })

            valid = [m for m in member_rows if m["window_change_pct"] is not None]
            valid_count = len(valid)
            member_count = len(symbols)
            if valid_count == 0:
                continue
            window_values = [m["window_change_pct"] for m in valid]
            avg_window = sum(window_values) / valid_count
            up_count = sum(v > 0 for v in window_values)
            avg_change = sum(m["change_pct"] for m in member_rows) / len(member_rows)
            # 组量比 = 有效成员量比的等权平均 (无量能数据的成员不参与, 全缺则维度不可判定)
            ratios = [m["volume_ratio"] for m in valid if m["volume_ratio"] is not None]
            group_ratio = sum(ratios) / len(ratios) if ratios else None

            members_pass = valid_count >= int(monitor["min_group_members"])
            breadth_pass = (up_count / valid_count) >= float(monitor["group_up_ratio"])
            momentum_pass = avg_window > 0
            change_pass = None if group_change_gate < 0 else avg_change > group_change_gate
            volume_pass = (
                None if group_volume_gate <= 0 or group_ratio is None
                else group_ratio >= group_volume_gate
            )
            qualifying = (
                members_pass and breadth_pass and momentum_pass
                and change_pass is not False and volume_pass is not False
            )
            leader = max(valid, key=lambda m: (m["window_change_pct"], m["change_pct"]))
            members_by_gid[group["id"]] = member_rows
            rows.append({
                "group_id": group["id"],
                "name": group["name"],
                "member_count": member_count,
                "valid_count": valid_count,
                "avg_change_pct": avg_change,
                "avg_window_pct": avg_window,
                "volume_ratio": group_ratio,
                "up_count": up_count,
                "up_ratio": up_count / valid_count,
                "qualifying": qualifying,
                "gates": {
                    "members": members_pass,
                    "breadth": breadth_pass,
                    "momentum": momentum_pass,
                    "change": change_pass,
                    "volume": volume_pass,
                },
                "leader": {
                    "symbol": leader["symbol"],
                    "name": leader["name"],
                    "change_pct": leader["change_pct"],
                    "window_change_pct": leader["window_change_pct"],
                    "volume_ratio": leader.get("volume_ratio"),
                },
            })

        rows.sort(key=lambda g: (g["avg_window_pct"] or 0.0), reverse=True)
        for rank, row in enumerate(rows, start=1):
            row["rank"] = rank
        return rows, members_by_gid

    def _resolve_name(self, symbol: str) -> str | None:
        """个股名称: enriched 通常不含 name, 从维表 name_map 兜底 (60s 缓存)。"""
        now = time.time()
        if now - self._name_map_ts > _NAME_MAP_TTL_SECONDS:
            try:
                self._name_map = self._repo.get_name_map() or {}
            except Exception:
                self._name_map = {}
            self._name_map_ts = now
        return self._name_map.get(symbol)

    @staticmethod
    def _prune(
        history: deque[tuple[float, float, float | None]],
        now: float,
        window_seconds: int,
    ) -> None:
        cutoff = now - window_seconds - _HISTORY_MARGIN_SECONDS
        while history and history[0][0] < cutoff:
            history.popleft()
