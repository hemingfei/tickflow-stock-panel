"""看板快照定时推送 — 在两个时间窗口内, 按固定间隔把看板快照 POST 到用户 Webhook。

快照内容见 app/api/overview.py 的 build_board_snapshot: 一个 JSON 覆盖「市场看板」
页面的全部信息 (overview 聚合 + 监控中心告警 + 数据概况), 下游前端/自动化可据此复现看板。

调度设计: 注册一个每分钟的常驻 CronTrigger job, 每次 fire 时重读偏好并在
Python 侧判断 —— 已启用、周一~周五、处于任一时间窗内 (北京时间, 闭区间,
时间窗数量可配置, 如早盘 + 午后两段避开午休)、且对齐所在窗口的间隔
(触发时刻恒为 该窗开始时间 + N*间隔 且 N>=1 —— 开始时刻只是计时起点,
本身不触发, 天级对齐, 重启/改配置不漂移) —— 全部满足才构建快照并发送。相比 IntervalTrigger: 改动配置只需写
preferences.json (job 每 fire 重读), 无需增删 job; 窗口外的 fire 只是一次
廉价的时间判断。
发送格式: format=feishu 按飞书自定义机器人规范发 interactive 卡片 (走
send_feishu_card); format=kol 按系统 KOL Webhook 简化格式发
{"text","title","msg_id"} (+可选 timestamp/sign 签名, 幂等防重发);
generic 原样 POST 快照 JSON (任意 http/s 下游)。
发送与快照构建都在线程池执行 (asyncio.to_thread), 不阻塞调度器事件循环。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from datetime import time as dt_time
from typing import Any

from app.market_time import cn_now

logger = logging.getLogger(__name__)

PUSH_JOB_ID = "board_webhook_push"


def parse_hhmm(text: str) -> dt_time:
    """解析 HH:MM 为 time; 非法格式抛 ValueError。"""
    return datetime.strptime((text or "").strip(), "%H:%M").time()


def parse_windows(cfg: dict) -> list[tuple[dt_time, dt_time, int]]:
    """从配置解析时间窗列表 [(开始, 结束, 间隔分钟)], 只保留完整合法的窗口。

    保存入口已拒绝半窗/非法格式, 这里静默跳过不完整或非法的窗口
    (fail-closed), 兼容手工编辑配置文件的场景。配置缺 windows 字段时
    (旧版平铺格式) 回退读取 start_time/end_time 等旧字段。
    """
    windows: list[tuple[dt_time, dt_time, int]] = []
    raw = cfg.get("windows")
    if isinstance(raw, list):
        for w in raw:
            if not isinstance(w, dict):
                continue
            s = str(w.get("start_time") or "").strip()
            e = str(w.get("end_time") or "").strip()
            if not s or not e:
                continue
            try:
                interval = int(w.get("interval_minutes") or 0)
                if interval <= 0:
                    continue
                windows.append((parse_hhmm(s), parse_hhmm(e), interval))
            except ValueError:
                continue
        return windows
    # 旧版平铺字段: 全局共用一个 interval_minutes
    try:
        interval = int(cfg.get("interval_minutes") or 0)
    except (TypeError, ValueError):
        interval = 0
    for s_key, e_key in (("start_time", "end_time"), ("start_time_2", "end_time_2")):
        s = str(cfg.get(s_key) or "").strip()
        e = str(cfg.get(e_key) or "").strip()
        if not s or not e:
            continue
        try:
            windows.append((parse_hhmm(s), parse_hhmm(e), interval))
        except ValueError:
            continue
    return windows


def in_push_window(now: dt_time, start: dt_time, end: dt_time) -> bool:
    """当前时刻是否在推送窗口内 (闭区间 [start, end])。

    start >= end 一律返回 False (fail-closed; 合法配置已在保存时强制 start < end)。
    """
    if start >= end:
        return False
    return start <= now <= end


def is_aligned_tick(now: datetime, start: dt_time, interval_minutes: int) -> bool:
    """当前时刻是否为对齐触发点: 距开始时间已过 N 个完整间隔 (N>=1)。

    开始时刻只是计时起点, 本身不触发 —— 首次推送在 开始时间 + 间隔。
    只按分钟粒度判断 (秒忽略) —— job 每分钟第 0 秒左右触发, 同一分钟内的
    迟到补跑仍命中同一对齐点。
    """
    if interval_minutes <= 0:
        return False
    start_sec = start.hour * 60 + start.minute
    now_sec = now.hour * 60 + now.minute
    elapsed = now_sec - start_sec
    return elapsed > 0 and elapsed % interval_minutes == 0


def should_push_now(now: datetime, cfg: dict) -> bool:
    """综合判断当前时刻是否应推送: 启用 + 工作日 + 处于任一时间窗内 + 对齐该窗间隔。

    每个窗口按自己的开始时间与间隔对齐 (开始 + N*间隔, N>=1, 开始本身不推送);
    周一~周五限制与现有交易时段判断 (intraday_sentiment.is_trading_time 等)
    口径一致; 项目无交易日历, 节假日靠快照数据自说明, 不做特殊处理。
    配置字段缺失/非法时返回 False (fail-closed), 不抛异常。
    """
    if not cfg.get("enabled"):
        return False
    if now.weekday() >= 5:
        return False
    t = now.time()
    for start, end, interval in parse_windows(cfg):
        if in_push_window(t, start, end) and is_aligned_tick(now, start, interval):
            return True
    return False


# ================================================================
# 推送状态 (内存态, 重启清零) — 供设置页展示最近推送结果
# ================================================================

_status_lock = threading.Lock()
_last_attempt_at: str | None = None
_last_success_at: str | None = None
_last_ok: bool | None = None
_last_detail: str = ""
_last_as_of: str | None = None
_last_format: str | None = None
_recent: deque[dict] = deque(maxlen=20)
# 同一分钟去重: misfire 补跑与正常触发可能落在同一分钟, 只推一次
_last_push_minute: str | None = None


def get_push_status() -> dict:
    with _status_lock:
        return {
            "last_attempt_at": _last_attempt_at,
            "last_success_at": _last_success_at,
            "last_ok": _last_ok,
            "last_detail": _last_detail,
            "last_as_of": _last_as_of,
            "last_format": _last_format,
            "recent": list(_recent),
        }


def _record_push_result(
    ok: bool, detail: str, as_of: str | None, duration_ms: int, fmt: str | None = None,
) -> None:
    now_iso = cn_now().isoformat(timespec="seconds")
    entry = {
        "time": now_iso,
        "ok": ok,
        "detail": detail,
        "as_of": as_of,
        "duration_ms": duration_ms,
        "format": fmt,
    }
    with _status_lock:
        global _last_attempt_at, _last_success_at, _last_ok, _last_detail, _last_as_of, _last_format
        _last_attempt_at = now_iso
        _last_ok = ok
        _last_detail = detail
        _last_as_of = as_of
        _last_format = fmt
        if ok:
            _last_success_at = now_iso
        _recent.appendleft(entry)


# ================================================================
# 快照构建与推送
# ================================================================

# 快照构建器由 main.py 注入 (api.overview.build_board_snapshot), 避免服务层
# 反向依赖 API 层; 测试可注入固定快照。
_snapshot_builder: Callable[[Any], dict] | None = None


def set_snapshot_builder(builder: Callable[[Any], dict]) -> None:
    global _snapshot_builder
    _snapshot_builder = builder


def _build_snapshot(app_state: Any) -> dict:
    if _snapshot_builder is None:
        raise RuntimeError("快照构建器未初始化 (应用尚未启动完成)")
    return _snapshot_builder(app_state)


# ================================================================
# 飞书卡片渲染 (format=feishu)
# ================================================================

def _finite(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _fmt_stock_pct(v: Any) -> str:
    """个股/概念涨跌幅: 小数制 → 百分数显示 (与看板 fmtStockPct 口径一致, x100)。"""
    f = _finite(v)
    return "—" if f is None else f"{f * 100:+.2f}%"


def _fmt_index_pct(v: Any) -> str:
    """指数涨跌幅: 后端已为百分数 (与看板 fmtIndexPct 口径一致, 不再 x100)。"""
    f = _finite(v)
    return "—" if f is None else f"{f:+.2f}%"


def _pct0(v: Any) -> str:
    f = _finite(v)
    return "—" if f is None else f"{f:.0f}%"


def _fmt_yuan(v: Any) -> str:
    """成交额 (元) → 亿/万亿 可读值 (与看板 fmtBigNum 口径一致)。"""
    f = _finite(v)
    if f is None:
        return "—"
    if f >= 1e12:
        return f"{f / 1e12:.2f}万亿"
    if f >= 1e8:
        return f"{f / 1e8:.2f}亿"
    if f >= 1e4:
        return f"{f / 1e4:.0f}万"
    return f"{f:.0f}"


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    """渲染 markdown 表格。单元格内的竖线/换行替换掉, 防止破坏表格结构。"""
    if not rows:
        return ""
    guard = str.maketrans({"|": "/", "\n": " "})
    head = "| " + " | ".join(headers) + " |"
    sep = "| " + " | ".join(["---"] * len(headers)) + " |"
    body = "\n".join(
        "| " + " | ".join(str(c).translate(guard) for c in cells) + " |"
        for cells in rows
    )
    return "\n".join([head, sep, body])


def _rank_leaders(item: dict) -> list[dict]:
    """取概念/行业条目的龙头股 (板块内涨幅前三); 兼容只有旧 leader 单字段的快照。"""
    leaders = item.get("leaders")
    if isinstance(leaders, list) and leaders:
        return leaders[:3]
    single = item.get("leader")
    if isinstance(single, dict) and (single.get("name") or single.get("symbol")):
        return [single]
    return []


def _rank_cell(item: dict) -> str:
    """行式摘要的板块条目: 名称 平均涨跌(龙头1/龙头2/龙头3)。"""
    base = f"{item.get('name')} {_fmt_stock_pct(item.get('avg_pct'))}"
    names = "/".join(
        str(ld.get("name") or ld.get("symbol") or "")
        for ld in _rank_leaders(item) if ld.get("name") or ld.get("symbol"))
    return f"{base}({names})" if names else base


def _render_board(snapshot: dict, bold: bool) -> str:
    """渲染看板快照核心区块 (指数含点位/涨跌/涨停梯队含高位股名/成交/趋势/榜单/概念与行业热度/最近告警)。

    bold=True 输出 markdown 加粗标签 (飞书卡片 lark_md); False 为纯文本
    (KOL Webhook 简化格式与富文本 post 都只提取纯文本, 加粗标记会原样露出)。
    字段单位经前端渲染逻辑核实, 不做启发式换算; 缺字段时显示 —, 不抛异常。
    """

    def _b(label: str) -> str:
        return f"**{label}**" if bold else label

    ov = snapshot.get("overview") if isinstance(snapshot.get("overview"), dict) else {}
    lines: list[str] = []

    dt_info = snapshot.get("data_time") if isinstance(snapshot.get("data_time"), dict) else {}
    dt_text = str(dt_info.get("text") or "").strip()
    if dt_text:
        lines.append(f"{_b('数据时间')} {dt_text}")

    emo = ov.get("emotion") or {}
    if emo:
        lines.append(f"{_b('情绪评分')}: {emo.get('label', '—')} {emo.get('score', '—')}")

    br = ov.get("breadth") or {}
    if br:
        lines.append(
            f"{_b('涨跌')} 上涨 {br.get('up', 0)} / 平 {br.get('flat', 0)} / 下跌 {br.get('down', 0)}"
            f" (上涨率 {_pct0(br.get('up_pct'))})"
            f" · 平均涨跌 {_fmt_stock_pct(br.get('avg_pct'))}")

    indices = ov.get("indices") or []
    if indices:
        parts = []
        for it in indices:
            name = it.get("name") or it.get("symbol") or "—"
            lp = _finite(it.get("last_price"))
            points = "—" if lp is None else f"{lp:.2f}"
            parts.append(f"{name} {points} {_fmt_index_pct(it.get('change_pct'))}")
        lines.append(f"{_b('指数')} {' · '.join(parts)}")

    lim = ov.get("limit") or {}
    if lim:
        lines.append(
            f"{_b('涨停')} {lim.get('limit_up', 0)} · 跌停 {lim.get('limit_down', 0)}"
            f" · 炸板 {lim.get('broken', 0)} (封板率 {_pct0(lim.get('seal_rate'))})"
            f" · 最高连板 {lim.get('max_boards', 0)}板")
        # 与首页 LadderMini 一致: 只看 ≥2 板、最多 6 档, 梯队内 ≤3 只时展示股名
        tiers = [t for t in (lim.get("tiers") or []) if (t.get("boards") or 0) >= 2]
        tparts = []
        for t in tiers[:6]:
            part = f"{t.get('boards')}板x{t.get('count')}"
            stocks = t.get("stocks") or []
            if 0 < len(stocks) <= 3:
                names = "·".join(
                    str(s.get("name") or s.get("symbol") or "")
                    for s in stocks[:3] if s.get("name") or s.get("symbol"))
                if names:
                    part = f"{part} {names}"
            tparts.append(part)
        if tparts:
            lines.append(f"梯队: {' | '.join(tparts)}")

    amt = ov.get("amount") or {}
    if amt:
        lines.append(f"{_b('成交额')} {_fmt_yuan(amt.get('total'))} (均额 {_fmt_yuan(amt.get('avg'))})")

    tr = ov.get("trend") or {}
    if tr:
        lines.append(
            f"{_b('趋势')} 站上MA5 {_pct0(tr.get('above_ma5_pct'))} · MA20 {_pct0(tr.get('above_ma20_pct'))}"
            f" · MA60 {_pct0(tr.get('above_ma60_pct'))}"
            f" · 新高 {tr.get('new_high', 0)} / 新低 {tr.get('new_low', 0)}")

    for rank_key, strong_label, weak_label in (
        ("concept_rank", "强势概念", "弱势概念"),
        ("industry_rank", "强势行业", "弱势行业"),
    ):
        ranks = ov.get(rank_key) or {}
        leading = ranks.get("leading") or []
        if leading:
            parts = [_rank_cell(c) for c in leading[:5]]
            lines.append(f"{_b(strong_label)} {' · '.join(parts)}")
        lagging = ranks.get("lagging") or []
        if lagging:
            parts = [_rank_cell(c) for c in lagging[:5]]
            lines.append(f"{_b(weak_label)} {' · '.join(parts)}")

    gainers = ov.get("top_gainers") or []
    if gainers:
        parts = [f"{g.get('name') or g.get('symbol')} {_fmt_stock_pct(g.get('change_pct'))}"
                 for g in gainers[:5]]
        lines.append(f"{_b('涨幅榜')} {' · '.join(parts)}")

    alerts = snapshot.get("alerts") or {}
    alert_items = alerts.get("alerts") or []
    if alert_items:
        lines.append(f"{_b('最近告警')} (近 7 天共 {alerts.get('total', len(alert_items))} 条)")
        for ev in alert_items[:5]:
            msg = str(ev.get("message") or "").strip()
            if msg:
                lines.append(f"- {msg}")

    return "\n".join(lines) if lines else "看板暂无数据"


def render_board_markdown(snapshot: dict) -> str:
    """飞书卡片版摘要 (lark_md 加粗标签, 行式布局 — lark_md 不支持表格语法)。"""
    return _render_board(snapshot, bold=True)


def render_board_text(snapshot: dict) -> str:
    """纯文本版摘要 (KOL Webhook 简化格式的 text 字段)。"""
    return _render_board(snapshot, bold=False)


def render_board_markdown_table(snapshot: dict) -> str:
    """表格版 markdown 摘要 — 供 KOL Webhook 等可渲染 markdown 的下游使用。

    与行式摘要同源同单位 (指数含点位、梯队含高位股名、概念/行业强弱、最近告警),
    用 markdown 表格组织, 排版更整洁。数据缺块时整表省略, 不产出空表。
    """
    ov = snapshot.get("overview") if isinstance(snapshot.get("overview"), dict) else {}
    sections: list[str] = []

    dt_info = snapshot.get("data_time") if isinstance(snapshot.get("data_time"), dict) else {}
    dt_text = str(dt_info.get("text") or "").strip()
    if dt_text:
        sections.append(f"**数据时间** {dt_text}")

    emo = ov.get("emotion") or {}
    if emo:
        sections.append(f"**情绪评分**: {emo.get('label', '—')} {emo.get('score', '—')}")

    # 市场概览 (KPI 表)
    kpi_rows: list[list[str]] = []
    br = ov.get("breadth") or {}
    if br:
        kpi_rows.append(["涨/平/跌", f"{br.get('up', 0)} / {br.get('flat', 0)} / {br.get('down', 0)}"
                                    f"(上涨率 {_pct0(br.get('up_pct'))})"])
        kpi_rows.append(["平均涨跌", _fmt_stock_pct(br.get("avg_pct"))])
    lim = ov.get("limit") or {}
    if lim:
        kpi_rows.append(["涨停 / 跌停 / 炸板", f"{lim.get('limit_up', 0)} / {lim.get('limit_down', 0)}"
                                              f" / {lim.get('broken', 0)}(封板率 {_pct0(lim.get('seal_rate'))})"])
        kpi_rows.append(["最高连板", f"{lim.get('max_boards', 0)}板"])
    amt = ov.get("amount") or {}
    if amt:
        kpi_rows.append(["成交额", f"{_fmt_yuan(amt.get('total'))}(均额 {_fmt_yuan(amt.get('avg'))})"])
    tr = ov.get("trend") or {}
    if tr:
        kpi_rows.append(["站上 MA5 / MA20 / MA60",
                         f"{_pct0(tr.get('above_ma5_pct'))} / {_pct0(tr.get('above_ma20_pct'))}"
                         f" / {_pct0(tr.get('above_ma60_pct'))}"])
        kpi_rows.append(["60日新高 / 新低", f"{tr.get('new_high', 0)} / {tr.get('new_low', 0)}"])
    act = ov.get("activity") or {}
    avg_turnover = _finite(act.get("avg_turnover"))
    vol_ratio = _finite(act.get("vol_ratio"))
    if avg_turnover is not None or vol_ratio is not None:
        turnover = "—" if avg_turnover is None else f"{avg_turnover:.1f}%"
        ratio = "—" if vol_ratio is None else f"{vol_ratio:.2f}"
        kpi_rows.append(["平均换手 / 量比", f"{turnover} / {ratio}"])
    if kpi_rows:
        sections.append("**市场概览**\n\n" + _md_table(["指标", "数值"], kpi_rows))

    # 指数 (点位 + 涨跌幅)
    indices = ov.get("indices") or []
    if indices:
        rows = []
        for it in indices:
            lp = _finite(it.get("last_price"))
            rows.append([
                str(it.get("name") or it.get("symbol") or "—"),
                "—" if lp is None else f"{lp:.2f}",
                _fmt_index_pct(it.get("change_pct")),
            ])
        sections.append("**指数**\n\n" + _md_table(["指数", "点位", "涨跌幅"], rows))

    # 涨停梯队 (≥2板, 高位优先, 每档展示成交额前 3 的股名)
    tiers = [t for t in (lim.get("tiers") or []) if (t.get("boards") or 0) >= 2]
    if tiers:
        rows = []
        for t in tiers[:6]:
            names = "、".join(
                str(s.get("name") or s.get("symbol") or "")
                for s in (t.get("stocks") or [])[:3] if s.get("name") or s.get("symbol"))
            rows.append([f"{t.get('boards')}板", str(t.get("count", 0)), names or "—"])
        sections.append("**涨停梯队**\n\n" + _md_table(["连板", "家数", "高位个股(前3)"], rows))

    # 概念 / 行业强弱
    for key, label in (("concept_rank", "概念"), ("industry_rank", "行业")):
        ranks = ov.get(key) or {}
        for rank_key, tag in (("leading", "强势"), ("lagging", "弱势")):
            items = ranks.get(rank_key) or []
            if not items:
                continue
            rows = []
            for c in items[:5]:
                names = "、".join(
                    str(ld.get("name") or ld.get("symbol") or "")
                    for ld in _rank_leaders(c) if ld.get("name") or ld.get("symbol"))
                rows.append([str(c.get("name") or "—"), _fmt_stock_pct(c.get("avg_pct")), names or "—"])
            sections.append(f"**{tag}{label}**\n\n" + _md_table([label, "平均涨跌", "龙头(前3)"], rows))

    # 最近告警 (列表)
    alerts = snapshot.get("alerts") or {}
    alert_items = alerts.get("alerts") or []
    if alert_items:
        bullets = "\n".join(f"- {ev.get('message')}" for ev in alert_items[:5]
                            if str(ev.get("message") or "").strip())
        if bullets:
            sections.append(f"**最近告警**(近 7 天共 {alerts.get('total', len(alert_items))} 条)\n\n{bullets}")

    # 涨幅榜 / 跌幅榜: 合并为一张四列表 (左涨幅右跌幅), 置于消息最后; 节标题见 **涨跌幅榜**
    gainers = (ov.get("top_gainers") or [])[:8]
    losers = (ov.get("top_losers") or [])[:8]
    if gainers or losers:
        rows = []
        for i in range(max(len(gainers), len(losers))):
            g = gainers[i] if i < len(gainers) else None
            lo = losers[i] if i < len(losers) else None
            rows.append([
                str(g.get("name") or g.get("symbol") or "—") if g else "",
                _fmt_stock_pct(g.get("change_pct")) if g else "",
                str(lo.get("name") or lo.get("symbol") or "—") if lo else "",
                _fmt_stock_pct(lo.get("change_pct")) if lo else "",
            ])
        sections.append("**涨跌幅榜**\n\n" + _md_table(["股票", "涨跌幅", "股票", "涨跌幅"], rows))

    return "\n\n".join(sections) if sections else "看板暂无数据"


def _send_feishu_board(url: str, secret: str, snapshot: dict) -> tuple[bool, str]:
    """按飞书自定义机器人规范把看板快照发为 interactive 卡片。"""
    from app.services import webhook_adapter

    if not webhook_adapter.is_valid_feishu_url(url):
        return False, "飞书格式需为飞书自定义机器人地址 (https://open.feishu.cn/open-apis/bot/v2/hook/...)"
    ov = snapshot.get("overview") if isinstance(snapshot.get("overview"), dict) else {}
    emo = ov.get("emotion") or {}
    as_of = snapshot.get("as_of") or "—"
    subtitle = f"{as_of}" + (f" · 情绪 {emo.get('label')} {emo.get('score')}/100" if emo else "")
    body = render_board_markdown(snapshot)
    ok = webhook_adapter.send_feishu_card(url, "市场看板快照", subtitle, body, secret)
    return ok, ("飞书卡片已发送" if ok else "飞书推送失败 (详见服务端日志)")


def _minute_stamp(snapshot: dict) -> str:
    """从快照 generated_at 取 HHMM 作为 msg_id 组成部分; 异常时退回当前北京时间。"""
    stamp = str(snapshot.get("generated_at") or "")
    if len(stamp) >= 16 and stamp[11:13].isdigit() and stamp[14:16].isdigit():
        return stamp[11:13] + stamp[14:16]
    return cn_now().strftime("%H%M")


def _send_kol_board(url: str, secret: str, snapshot: dict) -> tuple[bool, str]:
    """按系统 KOL Webhook 简化格式发送 (见 vpush「系统KOL-Webhook接入文档」)。

    请求体 {"text", "title", "msg_id"}; 配置了签名密钥时附加 timestamp + sign
    (签名算法与飞书自定义机器人一致, 复用 webhook_adapter._gen_sign)。
    msg_id 取 交易日+触发分钟 作为幂等键, 网络重试不会造成重复发帖。
    """
    from app.services import webhook_adapter
    from app.services.webhook_adapter import send_generic_webhook

    ov = snapshot.get("overview") if isinstance(snapshot.get("overview"), dict) else {}
    emo = ov.get("emotion") or {}
    as_of = snapshot.get("as_of") or "—"
    title = f"市场看板快照 {as_of}" + (f" · 情绪 {emo.get('label')} {emo.get('score')}/100" if emo else "")
    payload: dict = {
        # markdown 表格版摘要 (下游平台渲染 markdown, 排版更整洁), 正文上限 8000 字符
        "text": render_board_markdown_table(snapshot)[:8000],
        "title": title[:200],                          # 标题上限 200 字符 (超限自动截断)
        "msg_id": f"tickflow-board-{as_of}-{_minute_stamp(snapshot)}"[:128],
    }
    if secret:
        timestamp = str(int(time.time()))
        payload["timestamp"] = timestamp
        payload["sign"] = webhook_adapter._gen_sign(timestamp, secret)
    return send_generic_webhook(url, payload)


def push_now(app_state: Any, webhook_url: str | None = None, cfg: dict | None = None) -> dict:
    """立即构建一次看板快照并发送 (定时触发与「测试推送」共用)。

    format=feishu 按飞书自定义机器人规范发卡片摘要; 否则原样 POST 快照 JSON。

    Args:
        app_state:   FastAPI app.state (供快照构建器读取 repo / 服务单例)。
        webhook_url: 显式指定地址 (测试未保存的 URL), 否则用 cfg/偏好里的地址。
        cfg:         已读取的推送配置, 缺省时重读偏好。

    Returns:
        {ok, detail, as_of, snapshot_bytes, duration_ms}, 同时写入推送状态。
    """
    from app.services import preferences
    from app.services.webhook_adapter import is_valid_http_url, send_generic_webhook

    cfg = cfg or preferences.get_webhook_push_schedule()
    fmt = str(cfg.get("format") or "generic")
    url = (webhook_url or cfg.get("webhook_url") or "").strip()
    started = time.monotonic()

    if not is_valid_http_url(url):
        result = {"ok": False, "detail": "Webhook 地址未配置或非法", "as_of": None,
                  "snapshot_bytes": 0, "duration_ms": 0}
        _record_push_result(False, result["detail"], None, 0, fmt)
        return result

    as_of: str | None = None
    try:
        snapshot = _build_snapshot(app_state)
        as_of = snapshot.get("as_of") if isinstance(snapshot, dict) else None
        body = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
    except Exception as e:  # 快照构建失败计入状态, 不抛出
        logger.exception("看板快照构建失败")
        detail = f"快照构建失败: {e}"
        _record_push_result(False, detail, None, int((time.monotonic() - started) * 1000), fmt)
        return {"ok": False, "detail": detail, "as_of": None,
                "snapshot_bytes": 0, "duration_ms": int((time.monotonic() - started) * 1000)}

    secret = str(cfg.get("feishu_secret") or "")
    if fmt == "feishu":
        ok, send_detail = _send_feishu_board(url, secret, snapshot)
    elif fmt == "kol":
        ok, send_detail = _send_kol_board(url, secret, snapshot)
    else:
        ok, send_detail = send_generic_webhook(url, snapshot)
    duration_ms = int((time.monotonic() - started) * 1000)
    detail = f"{send_detail} · {len(body)} 字节"
    _record_push_result(ok, detail, as_of, duration_ms, fmt)
    if not ok:
        # 不打印 URL: Webhook 地址本身即凭据, 不得进日志
        logger.warning("看板快照推送失败: %s", send_detail)
    return {"ok": ok, "detail": detail, "as_of": as_of,
            "snapshot_bytes": len(body), "duration_ms": duration_ms}


def run_due_push(app_state: Any, now: datetime | None = None) -> dict | None:
    """定时 job 的同步执行体: 到点才推送, 未到点/禁用/同分钟重复一律跳过。

    Returns:
        推送结果 dict; 本轮未触发时为 None。
    """
    from app.services import preferences

    now = now or cn_now()
    cfg = preferences.get_webhook_push_schedule()
    if not should_push_now(now, cfg):
        return None

    minute_key = now.strftime("%Y-%m-%d %H:%M")
    with _status_lock:
        global _last_push_minute
        if _last_push_minute == minute_key:
            return None
        _last_push_minute = minute_key

    if not (cfg.get("webhook_url") or "").strip():
        # 保存入口已禁止 enabled 且无地址; 此处兜底 (手工改配置文件的场景)
        logger.warning("看板定时推送已启用但未配置 Webhook 地址, 跳过")
        return None

    return push_now(app_state, cfg=cfg)


async def run_due_push_async(app_state: Any) -> None:
    """APScheduler 协程 job: 重活放线程池, 不阻塞调度器事件循环。"""
    await asyncio.to_thread(run_due_push, app_state)


def register_push_job(scheduler, app_state: Any) -> None:
    """注册每分钟自检的推送 job (常驻, 配置变化无需重新注册)。"""
    from apscheduler.triggers.cron import CronTrigger

    # 注意: 必须传协程函数对象本身 (配合 args), 不能用 lambda 包裹,
    # 否则 APScheduler 按同步函数处理, 协程不会真正执行 (见 daily_pipeline 同类注释)。
    scheduler.add_job(
        run_due_push_async,
        args=[app_state],
        trigger=CronTrigger(minute="*", timezone="Asia/Shanghai"),
        id=PUSH_JOB_ID,
        misfire_grace_time=30,
        max_instances=1,
        replace_existing=True,
    )
