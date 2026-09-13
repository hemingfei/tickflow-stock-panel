// 指数共振页 — 监测指数上行, 在自选分组(板块)中找最强板块与最强个股。
// 数据契约: /api/resonance/state 的所有涨跌幅为百分数口径 (3.66 = 3.66%),
// 与指数侧一致; 页面内格式化不得再 ×100 (区别于 lib/format 的 fmtPct 小数制)。
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity, AlertTriangle, Pencil, Plus, RefreshCw, Trash2, Zap,
} from "lucide-react";
import {
  api,
  type ResonanceGate,
  type ResonanceMonitor,
  type ResonanceMonitorUpsert,
  type ResonanceStateRow,
} from "@/lib/api";
import { QK } from "@/lib/queryKeys";
import { PageHeader } from "@/components/PageHeader";
import { EmptyState } from "@/components/EmptyState";
import { Modal } from "@/components/Modal";
import { formatDuration, priceColorClass } from "@/lib/format";

const CORE_INDICES = [
  { symbol: "399006.SZ", name: "创业板指" },
  { symbol: "000680.SH", name: "科创综指" },
  { symbol: "000001.SH", name: "上证指数" },
  { symbol: "399001.SZ", name: "深证成指" },
];

// 动量窗口选项 (秒): 新增 30秒/1分钟/2分钟 覆盖开盘急拉的快节奏场景
const WINDOW_OPTIONS: { seconds: number; label: string }[] = [
  { seconds: 30, label: "30秒" },
  { seconds: 60, label: "1分钟" },
  { seconds: 120, label: "2分钟" },
  { seconds: 180, label: "3分钟" },
  { seconds: 300, label: "5分钟" },
  { seconds: 600, label: "10分钟" },
  { seconds: 900, label: "15分钟" },
  { seconds: 1200, label: "20分钟" },
  { seconds: 1800, label: "30分钟" },
];

function windowLabel(seconds: number | undefined): string {
  if (seconds == null || Number.isNaN(seconds)) return "—";
  if (seconds < 60 || seconds % 60 !== 0) return `${seconds}秒`;
  return `${Math.round(seconds / 60)}分钟`;
}

// 推送渠道 (与后端 NOTIFY_CHANNELS 一致; 地址在设置页配置)
const NOTIFY_CHANNEL_OPTIONS = [
  { id: "feishu", label: "飞书", hint: "需在设置中配置飞书机器人 Webhook" },
  { id: "wecom", label: "企业微信", hint: "需在设置中配置企业微信群机器人" },
  { id: "kol", label: "KOL", hint: "需在设置中配置 KOL Webhook" },
  { id: "custom", label: "自定义 Webhook", hint: "需在设置中配置自定义 Webhook 地址" },
  { id: "email", label: "邮件", hint: "需在设置中配置 SMTP" },
];

/** 百分数口径涨跌幅 (API 已是 3.66 形式), 不走 lib/format 的 fmtPct (小数制 ×100) */
function fmtPoint(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return "—";
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(digits)}%`;
}

function fmtRatio(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  return `${v.toFixed(2)}x`;
}

// 三维门禁结果 zh 标签 (true 过 / false 未过 / null 停用或不可判定)
const GATE_LABELS: Record<string, string> = {
  momentum: "动量", change: "涨幅", volume: "量能", members: "成员", breadth: "宽度",
};

function failedGates(gates: Record<string, ResonanceGate> | undefined | null): string[] {
  if (!gates) return [];
  return Object.entries(gates)
    .filter(([, v]) => v === false)
    .map(([k]) => GATE_LABELS[k] ?? k);
}

function minutesToHHMM(m: number): string {
  return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
}

function rangeLabel(ranges: { start: number; end: number }[]): string {
  if (!ranges.length) return "全时段";
  return ranges.map((r) => `${minutesToHHMM(r.start)}-${minutesToHHMM(r.end)}`).join(" / ");
}

const STATUS_META: Record<string, { label: string; className: string }> = {
  up: { label: "指数向上", className: "bg-bull/15 text-bull border-bull/30" },
  down: { label: "指数向下", className: "bg-bear/15 text-bear border-bear/30" },
  flat: { label: "震荡", className: "bg-elevated text-secondary border-border" },
  warming: { label: "预热中", className: "bg-amber-500/10 text-amber-500 border-amber-500/30" },
  off_window: { label: "时段外", className: "bg-elevated text-muted border-border" },
  no_data: { label: "数据未就绪", className: "bg-elevated text-muted border-border" },
  disabled: { label: "已停用", className: "bg-elevated text-muted border-border" },
};

function StatusBadge({ status }: { status: string }) {
  const meta = STATUS_META[status] ?? { label: status, className: "bg-elevated text-muted border-border" };
  return (
    <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium ${meta.className}`}>
      {meta.label}
    </span>
  );
}

// ── 监测卡片 ──────────────────────────────────────────────────

function MonitorCard({
  row,
  nowTs,
  onEdit,
  onDelete,
  onToggle,
  busy,
}: {
  row: ResonanceStateRow;
  /** 最新轮询的服务器时间戳 (秒), 用于计算共振持续时长 */
  nowTs: number;
  onEdit: () => void;
  onDelete: () => void;
  onToggle: () => void;
  busy: boolean;
}) {
  const { config, state } = row;
  const st = state;
  const index = st.index;
  const resonant = st.resonant;
  const resonanceFor = resonant && st.resonant_since != null && nowTs > 0
    ? formatDuration(Math.max(0, Math.round(nowTs - st.resonant_since)))
    : null;

  return (
    <div className={`rounded-card border bg-surface ${resonant ? "border-bull/50 shadow-[0_0_0_1px_rgba(239,68,68,0.15)]" : "border-border"}`}>
      {/* 卡片头: 名称 + 指数 + 状态 + 操作 */}
      <div className="flex flex-wrap items-center gap-2 px-4 py-3 border-b border-border/60">
        <span className="text-sm font-semibold text-foreground">{config.name}</span>
        <span className="text-xs text-muted">{index.name}</span>
        {resonant && (
          <span className="inline-flex items-center gap-1 rounded-full border border-bull/40 bg-bull/15 px-2 py-0.5 text-[11px] font-semibold text-bull">
            <Zap className="h-3 w-3" />
            共振中
          </span>
        )}
        <StatusBadge status={st.status} />
        <span className="text-[11px] text-muted">{rangeLabel(config.time_ranges)}</span>
        <div className="ml-auto flex items-center gap-1">
          <button
            type="button"
            onClick={onToggle}
            disabled={busy}
            title={config.enabled ? "停用监测" : "启用监测"}
            className={`px-2 py-1 rounded-btn text-[11px] border transition-colors disabled:opacity-40 ${
              config.enabled
                ? "border-border bg-elevated text-secondary hover:text-foreground"
                : "border-accent/40 bg-accent/10 text-accent hover:bg-accent/20"
            }`}
          >
            {config.enabled ? "停用" : "启用"}
          </button>
          <button
            type="button"
            onClick={onEdit}
            title="编辑监测"
            className="p-1.5 rounded-btn text-muted hover:text-foreground hover:bg-elevated transition-colors"
          >
            <Pencil className="h-3.5 w-3.5" />
          </button>
          <button
            type="button"
            onClick={onDelete}
            title="删除监测"
            className="p-1.5 rounded-btn text-muted hover:text-bear hover:bg-bear/10 transition-colors"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      {/* 指数行 */}
      <div className="flex flex-wrap items-baseline gap-x-5 gap-y-1 px-4 py-2.5">
        <span className="text-xs text-muted">指数</span>
        <span className="font-mono text-lg tabular-nums font-semibold">
          {index.price != null ? index.price.toFixed(2) : "—"}
        </span>
        <span className={`font-mono text-sm tabular-nums font-medium ${priceColorClass(index.change_pct)}`}>
          {fmtPoint(index.change_pct)}
        </span>
        <span className="text-xs text-muted">
          {windowLabel(config.window_seconds)}窗口
          <span className={`ml-1.5 font-mono tabular-nums ${priceColorClass(index.window_change_pct)}`}>
            {fmtPoint(index.window_change_pct)}
          </span>
        </span>
        <span className="text-xs text-muted">
          量比
          <span className="ml-1.5 font-mono tabular-nums text-secondary">{fmtRatio(index.volume_ratio)}</span>
        </span>
        {failedGates(index.gates).length > 0 && (
          <span className="text-[11px] text-bear">未过: {failedGates(index.gates).join(" / ")}</span>
        )}
        {resonanceFor && (
          <span className="text-xs text-bull">已持续 {resonanceFor}</span>
        )}
      </div>

      {/* 分组排行 */}
      {st.groups.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-muted border-b border-border/60">
                <th className="px-4 py-1.5 font-normal w-8">#</th>
                <th className="px-2 py-1.5 font-normal">板块分组</th>
                <th className="px-2 py-1.5 font-normal text-right">{windowLabel(config.window_seconds)}涨幅</th>
                <th className="px-2 py-1.5 font-normal text-right">上涨占比</th>
                <th className="px-2 py-1.5 font-normal text-right">量比</th>
                <th className="px-2 py-1.5 font-normal text-right">当前平均涨幅</th>
                <th className="px-2 py-1.5 font-normal">龙头</th>
              </tr>
            </thead>
            <tbody>
              {st.groups.map((g) => {
                const isTop = g.group_id === st.top_group_id && resonant;
                return (
                  <tr
                    key={g.group_id}
                    className={`border-b border-border/40 last:border-b-0 ${isTop ? "bg-bull/5" : ""}`}
                  >
                    <td className="px-4 py-1.5 text-muted tabular-nums">{g.rank}</td>
                    <td className="px-2 py-1.5">
                      <span className="text-foreground">{g.name}</span>
                      {isTop && (
                        <span className="ml-1.5 rounded-full bg-bull/15 text-bull px-1.5 py-px text-[10px] font-medium">
                          共振板块
                        </span>
                      )}
                      {!g.qualifying && failedGates(g.gates).length > 0 && (
                        <span className="ml-1.5 text-[10px] text-bear/80">
                          未达标: {failedGates(g.gates).join("/")}
                        </span>
                      )}
                      <span className="ml-1.5 text-[10px] text-muted">
                        {g.valid_count}/{g.member_count}只
                      </span>
                    </td>
                    <td className={`px-2 py-1.5 text-right font-mono tabular-nums ${priceColorClass(g.avg_window_pct)}`}>
                      {fmtPoint(g.avg_window_pct)}
                    </td>
                    <td className="px-2 py-1.5 text-right font-mono tabular-nums text-secondary">
                      {(g.up_ratio * 100).toFixed(0)}% ({g.up_count}/{g.valid_count})
                    </td>
                    <td className="px-2 py-1.5 text-right font-mono tabular-nums text-secondary">
                      {fmtRatio(g.volume_ratio)}
                    </td>
                    <td className={`px-2 py-1.5 text-right font-mono tabular-nums ${priceColorClass(g.avg_change_pct)}`}>
                      {fmtPoint(g.avg_change_pct)}
                    </td>
                    <td className="px-2 py-1.5">
                      {g.leader ? (
                        <span className="flex items-baseline gap-1.5">
                          <span className="text-secondary">{g.leader.name || g.leader.symbol}</span>
                          <span className="font-mono text-muted tabular-nums">{g.leader.symbol}</span>
                          <span className={`font-mono tabular-nums ${priceColorClass(g.leader.window_change_pct)}`}>
                            {fmtPoint(g.leader.window_change_pct)}
                          </span>
                          <span className="font-mono text-[10px] text-muted tabular-nums">
                            {fmtRatio(g.leader.volume_ratio)}
                          </span>
                        </span>
                      ) : (
                        <span className="text-muted/40">—</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="px-4 py-3 text-xs text-muted">
          {st.status === "warming"
            ? `窗口历史积累中 (约需 ${windowLabel(config.window_seconds)}), 分组涨幅将在历史足够后显示`
            : st.status === "off_window"
              ? "当前不在监测时段, 分组数据未计算"
              : "暂无分组数据 (检查监测的分组成员与行情覆盖)"}
        </div>
      )}

      {/* 共振结论 */}
      {resonant && st.leader && (
        <div className="flex flex-wrap items-center gap-2 px-4 py-2.5 border-t border-border/60 bg-bull/5 rounded-b-card">
          <Activity className="h-3.5 w-3.5 text-bull" />
          <span className="text-xs text-secondary">
            三级共振: <span className="text-foreground font-medium">{index.name}</span> 向上 ·
            板块 <span className="text-foreground font-medium">
              {st.groups.find((g) => g.group_id === st.top_group_id)?.name ?? st.top_group_id}
            </span> 最强 ·
            龙头 <span className="text-foreground font-medium">{st.leader.name || st.leader.symbol}</span>
          </span>
          <span className={`font-mono text-xs tabular-nums ${priceColorClass(st.leader.change_pct)}`}>
            {fmtPoint(st.leader.change_pct)}
          </span>
          <span className="font-mono text-xs text-muted tabular-nums">量比 {fmtRatio(st.leader.volume_ratio)}</span>
        </div>
      )}
    </div>
  );
}

// ── 配置弹窗 ──────────────────────────────────────────────────

interface DraftRange { start: string; end: string }

function MonitorConfigDialog({
  editing,
  onClose,
}: {
  editing: ResonanceMonitor | null;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const boards = useQuery({
    queryKey: QK.watchlistGroupBoards,
    queryFn: () => api.watchlistGroupBoards.list(),
  });

  const [name, setName] = useState(editing?.name ?? "");
  const [indexSymbol, setIndexSymbol] = useState(editing?.index_symbol ?? CORE_INDICES[0].symbol);
  const [enabled, setEnabled] = useState(editing?.enabled ?? true);
  const [windowSeconds, setWindowSeconds] = useState(editing?.window_seconds ?? 300);
  const [thresholdPct, setThresholdPct] = useState(
    String(editing?.index_threshold_pct ?? 0.3),
  );
  const [upRatioPct, setUpRatioPct] = useState(
    String(Math.round((editing?.group_up_ratio ?? 0.6) * 100)),
  );
  const [minMembers, setMinMembers] = useState(String(editing?.min_group_members ?? 3));
  const [indexChangeGate, setIndexChangeGate] = useState(String(editing?.index_change_pct_gate ?? 0.2));
  const [indexVolumeGate, setIndexVolumeGate] = useState(String(editing?.index_volume_ratio_gate ?? 1.5));
  const [groupChangeGate, setGroupChangeGate] = useState(String(editing?.group_change_pct_gate ?? 0));
  const [groupVolumeGate, setGroupVolumeGate] = useState(String(editing?.group_volume_ratio_gate ?? 1.3));
  const [leaderChangeGate, setLeaderChangeGate] = useState(String(editing?.leader_change_pct_gate ?? 0.5));
  const [leaderVolumeGate, setLeaderVolumeGate] = useState(String(editing?.leader_volume_ratio_gate ?? 1.5));
  const [channels, setChannels] = useState<string[]>(editing?.webhook_channels ?? []);
  const [cooldownMin, setCooldownMin] = useState(
    String(Math.round((editing?.notify_cooldown_seconds ?? 600) / 60)),
  );
  // 空列表 = 全部分组; 勾选任意 = 仅勾选分组
  const [selectedGroups, setSelectedGroups] = useState<string[]>(editing?.group_ids ?? []);
  const [limitTime, setLimitTime] = useState((editing?.time_ranges?.length ?? 0) > 0);
  const [ranges, setRanges] = useState<DraftRange[]>(
    (editing?.time_ranges ?? []).length > 0
      ? (editing?.time_ranges ?? []).map((r) => ({
          start: minutesToHHMM(r.start), end: minutesToHHMM(r.end),
        }))
      : [{ start: "09:30", end: "10:30" }],
  );

  const save = useMutation({
    mutationFn: (payload: ResonanceMonitorUpsert) =>
      editing
        ? api.resonance.updateMonitor(editing.id, payload)
        : api.resonance.createMonitor(payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.resonanceState });
      qc.invalidateQueries({ queryKey: QK.resonanceMonitors });
      onClose();
    },
  });

  // 校验 "HH:MM" 格式与 start < end; 仅做本地校验, 后端仍归一化为分钟数
  const rangesValid = useMemo(() => {
    if (!limitTime) return true;
    for (const r of ranges) {
      const s = /^(\d{2}):(\d{2})$/.exec(r.start);
      const e = /^(\d{2}):(\d{2})$/.exec(r.end);
      if (!s || !e) return false;
      if (Number(s[1]) * 60 + Number(s[2]) >= Number(e[1]) * 60 + Number(e[2])) return false;
    }
    return true;
  }, [limitTime, ranges]);

  const threshold = Number(thresholdPct);
  const ratio = Number(upRatioPct) / 100;
  const minMem = Number(minMembers);
  const gates: [string, number, number, (v: number) => boolean][] = [
    ["indexChangeGate", Number(indexChangeGate), -1, (v) => v >= -1 && v <= 20],
    ["indexVolumeGate", Number(indexVolumeGate), 0, (v) => v >= 0 && v <= 20],
    ["groupChangeGate", Number(groupChangeGate), -1, (v) => v >= -1 && v <= 20],
    ["groupVolumeGate", Number(groupVolumeGate), 0, (v) => v >= 0 && v <= 20],
    ["leaderChangeGate", Number(leaderChangeGate), -1, (v) => v >= -1 && v <= 20],
    ["leaderVolumeGate", Number(leaderVolumeGate), 0, (v) => v >= 0 && v <= 20],
  ];
  const cooldownSec = Number(cooldownMin) * 60;
  const valid =
    name.trim().length > 0
    && Number.isFinite(threshold) && threshold > 0
    && Number.isFinite(ratio) && ratio >= 0 && ratio <= 1
    && Number.isInteger(minMem) && minMem >= 2
    && gates.every(([, v, , check]) => Number.isFinite(v) && check(v))
    && Number.isFinite(cooldownSec) && cooldownSec >= 0 && cooldownSec <= 86400
    && rangesValid;

  const submit = () => {
    if (!valid) return;
    save.mutate({
      name: name.trim(),
      index_symbol: indexSymbol,
      enabled,
      time_ranges: limitTime
        ? ranges.map((r) => ({ start: r.start, end: r.end }))
        : [],
      window_seconds: windowSeconds,
      index_threshold_pct: threshold,
      group_up_ratio: ratio,
      min_group_members: minMem,
      group_ids: selectedGroups,
      index_change_pct_gate: Number(indexChangeGate),
      index_volume_ratio_gate: Number(indexVolumeGate),
      group_change_pct_gate: Number(groupChangeGate),
      group_volume_ratio_gate: Number(groupVolumeGate),
      leader_change_pct_gate: Number(leaderChangeGate),
      leader_volume_ratio_gate: Number(leaderVolumeGate),
      webhook_channels: channels,
      notify_cooldown_seconds: cooldownSec,
    });
  };

  const groupList = boards.data?.groups ?? [];
  const inputCls =
    "h-8 rounded-btn bg-elevated border border-border px-2.5 text-xs text-foreground focus:outline-none focus:border-accent/50";

  return (
    <Modal onClose={onClose} panelClassName="w-[92vw] max-w-xl bg-surface border border-border rounded-card shadow-xl" labelledBy="resonance-dialog-title">
      <div className="flex items-center justify-between px-4 py-3 border-b border-border">
        <h2 id="resonance-dialog-title" className="text-sm font-semibold text-foreground">
          {editing ? "编辑监测" : "新建指数共振监测"}
        </h2>
      </div>

      <div className="px-4 py-3 space-y-3 max-h-[70vh] overflow-y-auto">
        <div className="grid grid-cols-2 gap-3">
          <label className="block">
            <span className="mb-1 block text-xs text-muted">监测名称</span>
            <input
              className={`${inputCls} w-full`}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="如: 创业板共振"
              maxLength={50}
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs text-muted">监测指数</span>
            <select
              className={`${inputCls} w-full`}
              value={indexSymbol}
              onChange={(e) => setIndexSymbol(e.target.value)}
            >
              {CORE_INDICES.map((i) => (
                <option key={i.symbol} value={i.symbol}>{i.name}</option>
              ))}
            </select>
          </label>
        </div>

        <div>
          <p className="mb-1 text-[11px] font-medium text-secondary">指数判定 (动量 + 涨幅 + 量能)</p>
          <div className="grid grid-cols-4 gap-2">
            <label className="block">
              <span className="mb-1 block text-xs text-muted">动量窗口</span>
              <select
                className={`${inputCls} w-full`}
                value={windowSeconds}
                onChange={(e) => setWindowSeconds(Number(e.target.value))}
              >
                {WINDOW_OPTIONS.map((w) => (
                  <option key={w.seconds} value={w.seconds}>{w.label}</option>
                ))}
              </select>
            </label>
            <label className="block">
              <span className="mb-1 block text-xs text-muted">动量阈值(%)</span>
              <input
                className={`${inputCls} w-full`}
                type="number" step="0.05" min="0.01"
                value={thresholdPct}
                onChange={(e) => setThresholdPct(e.target.value)}
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs text-muted">涨幅≥(%)</span>
              <input
                className={`${inputCls} w-full`}
                type="number" step="0.05" min="-1"
                value={indexChangeGate}
                onChange={(e) => setIndexChangeGate(e.target.value)}
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs text-muted">量比≥(倍)</span>
              <input
                className={`${inputCls} w-full`}
                type="number" step="0.1" min="0"
                value={indexVolumeGate}
                onChange={(e) => setIndexVolumeGate(e.target.value)}
              />
            </label>
          </div>
        </div>

        <div>
          <p className="mb-1 text-[11px] font-medium text-secondary">板块达标门禁</p>
          <div className="grid grid-cols-4 gap-2">
            <label className="block">
              <span className="mb-1 block text-xs text-muted">板块效应(%)</span>
              <input
                className={`${inputCls} w-full`}
                type="number" step="5" min="0" max="100"
                value={upRatioPct}
                onChange={(e) => setUpRatioPct(e.target.value)}
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs text-muted">最少成员(只)</span>
              <input
                className={`${inputCls} w-full`}
                type="number" step="1" min="2"
                value={minMembers}
                onChange={(e) => setMinMembers(e.target.value)}
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs text-muted">涨幅≥(%)</span>
              <input
                className={`${inputCls} w-full`}
                type="number" step="0.1" min="-1"
                value={groupChangeGate}
                onChange={(e) => setGroupChangeGate(e.target.value)}
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs text-muted">量比≥(倍)</span>
              <input
                className={`${inputCls} w-full`}
                type="number" step="0.1" min="0"
                value={groupVolumeGate}
                onChange={(e) => setGroupVolumeGate(e.target.value)}
              />
            </label>
          </div>
        </div>

        <div>
          <p className="mb-1 text-[11px] font-medium text-secondary">龙头门禁 (共振板块内选最强)</p>
          <div className="grid grid-cols-4 gap-2">
            <label className="block">
              <span className="mb-1 block text-xs text-muted">涨幅≥(%)</span>
              <input
                className={`${inputCls} w-full`}
                type="number" step="0.1" min="-1"
                value={leaderChangeGate}
                onChange={(e) => setLeaderChangeGate(e.target.value)}
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs text-muted">量比≥(倍)</span>
              <input
                className={`${inputCls} w-full`}
                type="number" step="0.1" min="0"
                value={leaderVolumeGate}
                onChange={(e) => setLeaderVolumeGate(e.target.value)}
              />
            </label>
          </div>
        </div>
        <p className="text-[11px] text-muted leading-relaxed">
          所有维度为必要条件: 指数 = {windowLabel(windowSeconds)}动量 ≥ {thresholdPct || 0}% 且当前涨幅过门禁
          且窗口量比 ≥ {indexVolumeGate || 0} 倍; 板块 = 有效成员数、上涨占比 ≥ {upRatioPct || 0}%、
          平均窗口动量为正、当前平均涨幅与量比过门禁; 龙头 = 过门禁成员中窗口涨幅最强。
          涨幅门禁填 -1 停用, 量比门禁填 0 停用; 量比 = 窗口每分钟量 ÷ 当日每分钟平均量 (1.5 = 放量 50%)。
          窗口越短动量阈值应越小 (30秒窗口建议 0.1% 左右), 且依赖较快的行情轮询档位。
        </p>

        {/* 生效时段 */}
        <div className="rounded-card border border-border/60 p-3">
          <label className="flex items-center gap-2 text-xs text-secondary">
            <input
              type="checkbox"
              checked={limitTime}
              onChange={(e) => setLimitTime(e.target.checked)}
              className="accent-[hsl(var(--accent))]"
            />
            仅限指定时段生效 (不勾选 = 整个连续竞价时段)
          </label>
          {limitTime && (
            <div className="mt-2 space-y-2">
              {ranges.map((r, i) => (
                <div key={i} className="flex items-center gap-2">
                  <input
                    type="time"
                    className={inputCls}
                    value={r.start}
                    onChange={(e) => setRanges(ranges.map((x, j) => (j === i ? { ...x, start: e.target.value } : x)))}
                  />
                  <span className="text-xs text-muted">至</span>
                  <input
                    type="time"
                    className={inputCls}
                    value={r.end}
                    onChange={(e) => setRanges(ranges.map((x, j) => (j === i ? { ...x, end: e.target.value } : x)))}
                  />
                  {ranges.length > 1 && (
                    <button
                      type="button"
                      className="text-xs text-muted hover:text-bear"
                      onClick={() => setRanges(ranges.filter((_, j) => j !== i))}
                    >
                      移除
                    </button>
                  )}
                </div>
              ))}
              <button
                type="button"
                className="text-xs text-accent hover:underline"
                onClick={() => setRanges([...ranges, { start: "13:00", end: "14:00" }])}
              >
                + 添加时段
              </button>
            </div>
          )}
        </div>

        {/* 分组范围 */}
        <div className="rounded-card border border-border/60 p-3">
          <p className="text-xs text-secondary mb-2">
            参与比较的板块分组
            <span className="ml-1 text-muted">(不勾选 = 全部分组)</span>
          </p>
          {boards.isLoading ? (
            <p className="text-xs text-muted">分组加载中…</p>
          ) : groupList.length === 0 ? (
            <p className="text-xs text-muted">暂无自选分组, 可先在「自选板块」页创建</p>
          ) : (
            <div className="flex flex-wrap gap-2">
              {groupList.map((g) => {
                const checked = selectedGroups.includes(g.group_id);
                return (
                  <button
                    key={g.group_id}
                    type="button"
                    onClick={() =>
                      setSelectedGroups(
                        checked
                          ? selectedGroups.filter((id) => id !== g.group_id)
                          : [...selectedGroups, g.group_id],
                      )
                    }
                    className={`rounded-full border px-2.5 py-1 text-[11px] transition-colors ${
                      checked
                        ? "border-accent/40 bg-accent/10 text-accent"
                        : "border-border bg-elevated/40 text-secondary hover:bg-elevated"
                    }`}
                  >
                    {g.name}
                  </button>
                );
              })}
            </div>
          )}
        </div>

        <div>
          <p className="mb-1 text-[11px] font-medium text-secondary">推送通知</p>
          <div className="rounded-card border border-border/60 p-3 space-y-2">
            <div className="flex flex-wrap gap-2">
              {NOTIFY_CHANNEL_OPTIONS.map((opt) => {
                const checked = channels.includes(opt.id);
                return (
                  <button
                    key={opt.id}
                    type="button"
                    title={opt.hint}
                    onClick={() =>
                      setChannels(
                        checked
                          ? channels.filter((c) => c !== opt.id)
                          : [...channels, opt.id],
                      )
                    }
                    className={`rounded-full border px-2.5 py-1 text-[11px] transition-colors ${
                      checked
                        ? "border-accent/40 bg-accent/10 text-accent"
                        : "border-border bg-elevated/40 text-secondary hover:bg-elevated"
                    }`}
                  >
                    {opt.label}
                  </button>
                );
              })}
            </div>
            <div className="flex items-center gap-2">
              <span className="text-xs text-muted">通知冷却</span>
              <input
                className={`${inputCls} w-24`}
                type="number" step="1" min="0" max="1440"
                value={cooldownMin}
                onChange={(e) => setCooldownMin(e.target.value)}
              />
              <span className="text-xs text-muted">分钟 (0 = 仅共振出现时通知一次)</span>
            </div>
            <p className="text-[11px] text-muted leading-relaxed">
              共振出现时立即推送一次 (持续共振不重复); 指数反复翻飞时按冷却时间去重。
              渠道需先在「设置 → 通知」配置地址; 站内弹窗与系统通知由全局开关控制, 不受此处勾选影响。
            </p>
          </div>
        </div>

        <label className="flex items-center gap-2 text-xs text-secondary">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
            className="accent-[hsl(var(--accent))]"
          />
          启用监测 (停用后不计算, 也不产生共振状态)
        </label>
      </div>

      <div className="flex items-center justify-end gap-2 px-4 py-3 border-t border-border">
        {save.isError && (
          <span className="mr-auto flex items-center gap-1 text-xs text-bear">
            <AlertTriangle className="h-3.5 w-3.5" />
            保存失败, 请检查配置
          </span>
        )}
        <button
          type="button"
          onClick={onClose}
          className="px-3 py-1.5 rounded-btn border border-border text-xs text-secondary hover:text-foreground hover:bg-elevated transition-colors"
        >
          取消
        </button>
        <button
          type="button"
          onClick={submit}
          disabled={!valid || save.isPending}
          className="px-4 py-1.5 rounded-btn bg-accent/90 text-base text-xs font-medium hover:bg-accent disabled:opacity-50 transition-colors"
        >
          {save.isPending ? "保存中…" : editing ? "保存" : "创建"}
        </button>
      </div>
    </Modal>
  );
}

// ── 页面 ──────────────────────────────────────────────────────

export function IndexResonance() {
  const qc = useQueryClient();
  const state = useQuery({
    queryKey: QK.resonanceState,
    queryFn: () => api.resonance.state(),
    refetchInterval: 5000,
    placeholderData: (prev) => prev,
  });
  const [dialog, setDialog] = useState<{ open: boolean; editing: ResonanceMonitor | null }>({
    open: false, editing: null,
  });
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: QK.resonanceState });
    qc.invalidateQueries({ queryKey: QK.resonanceMonitors });
  };

  const toggle = useMutation({
    mutationFn: (row: ResonanceStateRow) => {
      const c = row.config;
      return api.resonance.updateMonitor(c.id, {
        name: c.name,
        index_symbol: c.index_symbol,
        enabled: !c.enabled,
        time_ranges: c.time_ranges.map((r) => ({
          start: minutesToHHMM(r.start), end: minutesToHHMM(r.end),
        })),
        window_seconds: c.window_seconds,
        index_threshold_pct: c.index_threshold_pct,
        group_up_ratio: c.group_up_ratio,
        min_group_members: c.min_group_members,
        group_ids: c.group_ids,
        index_change_pct_gate: c.index_change_pct_gate,
        index_volume_ratio_gate: c.index_volume_ratio_gate,
        group_change_pct_gate: c.group_change_pct_gate,
        group_volume_ratio_gate: c.group_volume_ratio_gate,
        leader_change_pct_gate: c.leader_change_pct_gate,
        leader_volume_ratio_gate: c.leader_volume_ratio_gate,
        webhook_channels: c.webhook_channels,
        notify_cooldown_seconds: c.notify_cooldown_seconds,
      });
    },
    onSuccess: invalidate,
  });

  const remove = useMutation({
    mutationFn: (monitorId: string) => api.resonance.deleteMonitor(monitorId),
    onSuccess: () => {
      setConfirmDeleteId(null);
      invalidate();
    },
  });

  const rows = state.data?.monitors ?? [];
  const resonantCount = rows.filter((r) => r.state.resonant).length;

  return (
    <div className="h-full flex flex-col">
      <PageHeader
        title="指数共振"
        subtitle="指数上行 → 最强板块 → 最强个股"
        titleExtra={
          resonantCount > 0 ? (
            <span className="inline-flex items-center gap-1 rounded-full border border-bull/40 bg-bull/15 px-2 py-0.5 text-[11px] font-semibold text-bull">
              <Zap className="h-3 w-3" />
              {resonantCount} 个共振中
            </span>
          ) : undefined
        }
        right={
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => state.refetch()}
              title="刷新"
              className="p-2 rounded-btn text-secondary hover:text-foreground hover:bg-elevated transition-colors"
            >
              <RefreshCw className={`h-4 w-4 ${state.isFetching ? "animate-spin" : ""}`} />
            </button>
            <button
              type="button"
              onClick={() => setDialog({ open: true, editing: null })}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn bg-accent/90 text-base text-xs font-medium hover:bg-accent transition-colors"
            >
              <Plus className="h-3.5 w-3.5" />
              新建监测
            </button>
          </div>
        }
      />

      <div className="flex-1 min-h-0 overflow-y-auto p-4 space-y-3">
        {state.isLoading ? (
          <div className="grid place-items-center py-16 text-sm text-muted">加载中…</div>
        ) : state.isError ? (
          <div className="rounded-card border border-border bg-surface px-4 py-8 text-center">
            <AlertTriangle className="mx-auto h-8 w-8 text-bear" />
            <p className="mt-2 text-sm text-secondary">状态加载失败, 请确认后端服务正常</p>
            <button
              type="button"
              onClick={() => state.refetch()}
              className="mt-3 px-3 py-1.5 rounded-btn border border-border text-xs text-secondary hover:text-foreground hover:bg-elevated"
            >
              重试
            </button>
          </div>
        ) : rows.length === 0 ? (
          <EmptyState
            icon={Activity}
            title="还没有指数共振监测"
            hint="创建一个监测: 选择指数 (创业板 / 科创板 / 上证 / 深证) 与监测时段, 指数向上时自动在自选分组中排出最强板块与龙头个股。支持创建多个监测同时运行。"
          />
        ) : (
          rows.map((row) => (
            <MonitorCard
              key={row.config.id}
              row={row}
              nowTs={state.data?.ts ?? 0}
              busy={toggle.isPending || remove.isPending}
              onEdit={() => setDialog({ open: true, editing: row.config })}
              onDelete={() => setConfirmDeleteId(row.config.id)}
              onToggle={() => toggle.mutate(row)}
            />
          ))
        )}
      </div>

      {dialog.open && (
        <MonitorConfigDialog
          editing={dialog.editing}
          onClose={() => setDialog({ open: false, editing: null })}
        />
      )}

      {/* 删除二次确认 */}
      {confirmDeleteId && (
        <Modal
          onClose={() => setConfirmDeleteId(null)}
          ariaLabel="确认删除监测"
          panelClassName="w-[88vw] max-w-sm bg-surface border border-border rounded-card shadow-xl"
        >
          <div className="px-4 py-4">
            <p className="text-sm text-foreground">确认删除该监测?</p>
            <p className="mt-1 text-xs text-muted">删除后其共振历史与配置不可恢复。</p>
            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setConfirmDeleteId(null)}
                className="px-3 py-1.5 rounded-btn border border-border text-xs text-secondary hover:text-foreground hover:bg-elevated"
              >
                取消
              </button>
              <button
                type="button"
                onClick={() => remove.mutate(confirmDeleteId)}
                disabled={remove.isPending}
                className="px-3 py-1.5 rounded-btn bg-bear/90 text-xs font-medium text-white hover:bg-bear disabled:opacity-50"
              >
                {remove.isPending ? "删除中…" : "确认删除"}
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}
