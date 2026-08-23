import { useMemo, useState } from "react";
import { Activity, Loader2, Lock, RefreshCw } from "lucide-react";
import { api, type IndexQuote, type MinuteKlineRow, type KlineRow } from "@/lib/api";
import { QK } from "@/lib/queryKeys";
import { useCapabilities } from "@/lib/useSharedQueries";
import { EChartsIntraday } from "@/components/EChartsIntraday";
import { EChartsCandlestick, type OHLC } from "@/components/EChartsCandlestick";
import { useQuery, useQueryClient } from "@tanstack/react-query";

const CORE_INDICES = [
  { symbol: "000001.SH", name: "上证指数" },
  { symbol: "399001.SZ", name: "深证成指" },
  { symbol: "399006.SZ", name: "创业板指" },
  { symbol: "000680.SH", name: "科创综指" },
];

type PeriodType = "分时" | "1" | "5" | "15" | "30" | "60" | "D" | "W" | "M";

const PERIODS: { value: PeriodType; label: string }[] = [
  { value: "分时", label: "分时" },
  { value: "1", label: "1分K" },
  { value: "5", label: "5分K" },
  { value: "15", label: "15分K" },
  { value: "30", label: "30分K" },
  { value: "60", label: "60分K" },
  { value: "D", label: "日K" },
  { value: "W", label: "周K" },
  { value: "M", label: "月K" },
];

// Moving average periods we need to calculate
const MA_PERIODS = [5, 8, 13, 55, 60, 65, 120];

// Helper to format numbers
function fmtNum(v: number | null | undefined, digits = 2) {
  if (v == null || Number.isNaN(Number(v))) return "--";
  return Number(v).toFixed(digits);
}

// Helper to determine which MAs to show for each period
function getMASettings(period: PeriodType) {
  switch (period) {
    case "1":
    case "5":
    case "15":
    case "30":
    case "60":
      // Minute K-lines: show only M5
      return { showMA: true, periods: [5] };
    case "W":
    case "M":
      // Weekly/Monthly: show M5, M10
      return { showMA: true, periods: [5, 10] };
    case "D":
      // Daily: show all
      return { showMA: true, periods: [5, 8, 13, 55, 60, 65, 120] };
    case "分时":
    default:
      return { showMA: false, periods: [] };
  }
}

// Calculate moving averages for OHLC data with specific periods
function calculateMovingAverages(data: OHLC[], periods: number[] = MA_PERIODS): OHLC[] {
  if (!data || data.length === 0) return data;

  const result: OHLC[] = [...data];

  periods.forEach((period) => {
    for (let i = 0; i < result.length; i++) {
      if (i < period - 1) {
        // Not enough data points for this MA, set to null
        (result[i] as any)[`ma${period}`] = null;
      } else {
        // Calculate the average
        let sum = 0;
        for (let j = i - period + 1; j <= i; j++) {
          sum += result[j].close;
        }
        (result[i] as any)[`ma${period}`] = sum / period;
      }
    }
  });

  return result;
}

// Aggregate minute K-lines to higher periods
function aggregateMinuteKlines(
  minuteData: MinuteKlineRow[],
  period: PeriodType
): OHLC[] {
  if (!minuteData || minuteData.length === 0) return [];

  // For 1-minute period, just convert format - 直接从datetime提取时间，和EChartsIntraday保持一致
  if (period === "1") {
    return minuteData.map((row) => {
      const dt = new Date(row.datetime);
      // 不做时区转换，直接使用本地时间
      const hours = String(dt.getHours()).padStart(2, "0");
      const minutes = String(dt.getMinutes()).padStart(2, "0");
      const dateStr = `${hours}:${minutes}`;
      return {
        date: dateStr,
        open: Number(row.open),
        high: Number(row.high),
        low: Number(row.low),
        close: Number(row.close),
        volume: Number(row.volume || 0),
      };
    });
  }

  // Determine the number of minutes to aggregate
  let windowMinutes: number;
  switch (period) {
    case "5": windowMinutes = 5; break;
    case "15": windowMinutes = 15; break;
    case "30": windowMinutes = 30; break;
    case "60": windowMinutes = 60; break;
    default: windowMinutes = 5;
  }

  // Sort data by datetime
  const sortedData = [...minuteData].sort((a, b) =>
    new Date(a.datetime).getTime() - new Date(b.datetime).getTime()
  );

  const result: OHLC[] = [];
  let currentOpen: number | null = null;
  let currentHigh: number | null = null;
  let currentLow: number | null = null;
  let currentClose: number | null = null;
  let currentVolume = 0;
  let windowStart: Date | null = null;

  for (let i = 0; i < sortedData.length; i++) {
    const row = sortedData[i];
    const dt = new Date(row.datetime);

    // Calculate the window start time for the current period - 不做时区转换
    let newWindowStart: Date;

    const minutesSinceOpen = (dt.getHours() - 9) * 60 + (dt.getMinutes() - 30);
    const totalMinutesInWindow = Math.floor(minutesSinceOpen / windowMinutes) * windowMinutes;

    const hours = 9 + Math.floor((totalMinutesInWindow + 30) / 60);
    const minutes = (totalMinutesInWindow + 30) % 60;

    newWindowStart = new Date(dt.getFullYear(), dt.getMonth(), dt.getDate(), hours, minutes, 0, 0);

	  // If this is the first data point or a new window starts
    if (!windowStart || newWindowStart.getTime() !== windowStart.getTime()) {
      // If we have an ongoing window, save it
      if (windowStart && currentClose !== null) {
        const year = String(windowStart.getFullYear());
        const month = String(windowStart.getMonth() + 1).padStart(2, "0");
        const day = String(windowStart.getDate()).padStart(2, "0");
        const hours = String(windowStart.getHours()).padStart(2, "0");
        const minutes = String(windowStart.getMinutes()).padStart(2, "0");
        const dateStr = `${year}-${month}-${day} ${hours}:${minutes}`;
        result.push({
          date: dateStr,
          open: currentOpen || 0,
          high: currentHigh || 0,
          low: currentLow || 0,
          close: currentClose,
          volume: currentVolume,
        });
      }

      // Start a new window
      windowStart = newWindowStart;
      currentOpen = Number(row.open);
      currentHigh = Number(row.high);
      currentLow = Number(row.low);
      currentClose = Number(row.close);
      currentVolume = Number(row.volume || 0);
    } else {
      // Continue the current window
      currentHigh = Math.max(currentHigh || 0, Number(row.high));
      currentLow = Math.min(currentLow || Infinity, Number(row.low));
      currentClose = Number(row.close);
      currentVolume += Number(row.volume || 0);
    }
  }

	  // Save the last window
  if (windowStart && currentClose !== null) {
    const year = String(windowStart.getFullYear());
    const month = String(windowStart.getMonth() + 1).padStart(2, "0");
    const day = String(windowStart.getDate()).padStart(2, "0");
    const hours = String(windowStart.getHours()).padStart(2, "0");
    const minutes = String(windowStart.getMinutes()).padStart(2, "0");
    const dateStr = `${year}-${month}-${day} ${hours}:${minutes}`;
    result.push({
      date: dateStr,
      open: currentOpen || 0,
      high: currentHigh || 0,
      low: currentLow || 0,
      close: currentClose,
      volume: currentVolume,
    });
  }

  return result;
}

// Convert daily K-line data to weekly/monthly
function aggregateDailyKlines(
  dailyData: KlineRow[],
  period: "W" | "M"
): OHLC[] {
  if (!dailyData || dailyData.length === 0) return [];

  const sortedData = [...dailyData].sort((a, b) =>
    new Date(a.date).getTime() - new Date(b.date).getTime()
  );

  const result: OHLC[] = [];
  let currentOpen: number | null = null;
  let currentHigh: number | null = null;
  let currentLow: number | null = null;
  let currentClose: number | null = null;
  let currentVolume = 0;
  let windowKey: string | null = null;

  for (let i = 0; i < sortedData.length; i++) {
    const row = sortedData[i];
    const dt = new Date(row.date);

    // Calculate window key
    let newWindowKey: string;
    if (period === "W") {
      // For weekly, use week number
      const startOfWeek = new Date(dt);
      startOfWeek.setDate(dt.getDate() - dt.getDay());
      newWindowKey = `${startOfWeek.getFullYear()}-${String(startOfWeek.getMonth() + 1).padStart(2, "0")}-${String(startOfWeek.getDate()).padStart(2, "0")}`;
    } else {
      // For monthly, use year-month
      newWindowKey = `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}`;
    }

    // If new window starts
    if (newWindowKey !== windowKey) {
      // Save the previous window if it exists
      if (windowKey && currentClose !== null) {
        result.push({
          date: windowKey,
          open: currentOpen || 0,
          high: currentHigh || 0,
          low: currentLow || 0,
          close: currentClose,
          volume: currentVolume,
        });
      }

      // Start new window
      windowKey = newWindowKey;
      currentOpen = Number(row.open);
      currentHigh = Number(row.high);
      currentLow = Number(row.low);
      currentClose = Number(row.close);
      currentVolume = Number(row.volume || 0);
    } else {
      // Continue current window
      currentHigh = Math.max(currentHigh || 0, Number(row.high));
      currentLow = Math.min(currentLow || Infinity, Number(row.low));
      currentClose = Number(row.close);
      currentVolume += Number(row.volume || 0);
    }
  }

  // Add the last window
  if (windowKey && currentClose !== null) {
    result.push({
      date: windowKey,
      open: currentOpen || 0,
      high: currentHigh || 0,
      low: currentLow || 0,
      close: currentClose,
      volume: currentVolume,
    });
  }

  return result;
}

// Format daily data for display
function toOHLC(rows: KlineRow[]): OHLC[] {
  return rows
    .filter((r) => r?.date != null && r.open != null && r.close != null)
    .map((r) => ({
      date: typeof r.date === "string" ? r.date : String(r.date),
      open: Number(r.open),
      high: Number(r.high),
      low: Number(r.low),
      close: Number(r.close),
      volume: Number(r.volume || 0),
    }));
}

interface IndexCardProps {
  symbol: string;
  name: string;
  quote?: IndexQuote;
  minuteData?: MinuteKlineRow[];
  dailyData?: KlineRow[];
  minuteLoading?: boolean;
  dailyLoading?: boolean;
  hasMinuteCap: boolean;
  period: PeriodType;
  onPeriodChange: (p: PeriodType) => void;
}

function IndexCard({
  symbol,
  name,
  quote,
  minuteData,
  dailyData,
  minuteLoading,
  dailyLoading,
  hasMinuteCap,
  period,
  onPeriodChange,
}: IndexCardProps) {
  const current = quote?.last_price ?? quote?.price ?? quote?.close
  const changePct = quote?.change_pct ?? quote?.pct
  const prevClose = quote?.prev_close ?? quote?.close ?? undefined

  // Determine which data to use based on period
  const isMinutePeriod = ["1", "5", "15", "30", "60"].includes(period)
  const isDailyPeriod = ["D", "W", "M"].includes(period)
  
  // Get MA settings for current period
  const maSettings = getMASettings(period)

  // Process chart data
  const chartData = useMemo(() => {
    if (isMinutePeriod && hasMinuteCap) {
      return aggregateMinuteKlines(minuteData || [], period)
    } else if (isDailyPeriod) {
      const dailyOhlc = toOHLC(dailyData || [])
      if (period === "W") {
        return aggregateDailyKlines(dailyOhlc, "W")
      } else if (period === "M") {
        return aggregateDailyKlines(dailyOhlc, "M")
      }
      return dailyOhlc
    }
    return []
  }, [minuteData, dailyData, period, isMinutePeriod, isDailyPeriod, hasMinuteCap])

  // Calculate moving averages based on period settings
  const chartDataWithMA = useMemo(() => 
    calculateMovingAverages(chartData, maSettings.periods), 
    [chartData, maSettings.periods])

  return (
    <div className="rounded-card border border-border bg-surface p-3">
      <div className="mb-2 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Activity className="h-4 w-4 text-accent" />
          <h3 className="text-sm font-semibold text-foreground">{name}</h3>
          <span className="font-mono text-xs text-muted">{symbol}</span>
        </div>
        <div className="flex items-center gap-2">
          <span className="font-mono text-sm text-foreground">{fmtNum(current)}</span>
          <span className={`font-mono text-sm ${Number(changePct ?? 0) >= 0 ? "text-bull" : "text-bear"}`}>
            {changePct != null ? `${Number(changePct) >= 0 ? "+" : ""}${Number(changePct).toFixed(2)}%` : "--"}
          </span>
        </div>
      </div>

      {/* Period selector */}
      <div className="mb-2 flex flex-wrap gap-1">
        {PERIODS.map((p) => {
          const disabled = (p.value !== "分时" && p.value !== "D" && p.value !== "W" && p.value !== "M" && !hasMinuteCap)
          return (
            <button
              key={p.value}
              onClick={() => onPeriodChange(p.value)}
              disabled={disabled}
              className={`px-2 py-1 text-xs rounded transition-colors ${
                disabled
                  ? "text-muted cursor-not-allowed opacity-50"
                  : period === p.value
                  ? "bg-accent text-white"
                  : "bg-surface border border-border hover:bg-elevated"
              }`}
            >
              {p.label}
            </button>
          )
        })}
      </div>

      {/* Chart */}
      {period !== "分时" && period !== "D" && period !== "W" && period !== "M" && !hasMinuteCap ? (
        <div className="flex h-64 flex-col items-center justify-center gap-2 text-center">
          <Lock className="h-5 w-5 text-muted" />
          <div className="text-xs text-secondary">分时数据权限需 Pro+</div>
        </div>
      ) : (period === "分时" && minuteLoading) || (isDailyPeriod && dailyLoading) ? (
        <div className="flex h-64 items-center justify-center">
          <Loader2 className="h-5 w-5 animate-spin text-muted" />
          <span className="ml-2 text-xs text-muted">数据加载中…</span>
        </div>
      ) : period === "分时" && (!minuteData || minuteData.length === 0) ? (
        <div className="flex h-64 items-center justify-center text-xs text-muted">
          暂无数据
        </div>
      ) : period === "分时" ? (
        <div className="h-80">
          <EChartsIntraday
            data={minuteData || []}
            height={320}
            prevClose={prevClose}
            showLimitLines={false}
            showAvgLine={false}
            isIndex={true}
          />
        </div>
      ) : isMinutePeriod ? (
        <div className="h-80">
          <EChartsCandlestick
            data={chartDataWithMA}
            height={320}
            showMA={maSettings.showMA}
            showInfoBar={true}
            activeIndicators={["vol"]}
          />
        </div>
      ) : (
        <div className="h-80">
          <EChartsCandlestick
            data={chartDataWithMA}
            height={320}
            showMA={maSettings.showMA}
            showInfoBar={true}
            activeIndicators={[]}
          />
        </div>
      )}
    </div>
  )
}

export function LiveIndices() {
  const qc = useQueryClient();
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [periods, setPeriods] = useState<Record<string, PeriodType>>({
    "000001.SH": "分时",
    "399001.SZ": "分时",
    "399006.SZ": "分时",
    "000680.SH": "分时",
  });

  //分时数据需 Pro+ (kline.minute.batch) 能力
  const caps = useCapabilities();
  const hasMinuteCap = !!caps.data?.capabilities?.["kline.minute.batch"];

  const quotes = useQuery({
    queryKey: QK.indexQuotes,
    queryFn: () => api.indexQuotes(CORE_INDICES.map((i) => i.symbol)),
    placeholderData: (prev) => prev,
    refetchInterval: 5000,
  });

  const batchMinute = useQuery({
    queryKey: QK.indexMinuteBatch(CORE_INDICES.map((i) => i.symbol)),
    queryFn: () => api.klineMinuteBatch(CORE_INDICES.map((i) => i.symbol)),
    enabled: hasMinuteCap,
    placeholderData: (prev) => prev,
    refetchInterval: 5000,
  });

  // Fetch daily data for each index
  const dailyQueries = useQuery({
    queryKey: ["indexDailyBatch", ...CORE_INDICES.map((i) => i.symbol)],
    queryFn: async () => {
      const results: Record<string, KlineRow[]> = {};
      for (const index of CORE_INDICES) {
        try {
          const data = await api.indexDaily(index.symbol, 120);
          results[index.symbol] = data.rows || [];
        } catch (e) {
          results[index.symbol] = [];
        }
      }
      return results;
    },
  });

  const quoteBySymbol = useMemo(() => {
    const m = new Map<string, IndexQuote>();
    for (const q of quotes.data?.rows ?? []) m.set(q.symbol, q);
    return m;
  }, [quotes.data?.rows]);

  const minuteBySymbol = useMemo(() => {
    const m = new Map<string, MinuteKlineRow[]>();
    const data = batchMinute.data?.data;
    if (data) {
      for (const [symbol, rows] of Object.entries(data)) {
        m.set(symbol, rows || []);
      }
    }
    return m;
  }, [batchMinute.data?.data]);

  const dailyBySymbol = useMemo(() => {
    const m = new Map<string, KlineRow[]>();
    const data = dailyQueries.data;
    if (data) {
      for (const [symbol, rows] of Object.entries(data)) {
        m.set(symbol, rows || []);
      }
    }
    return m;
  }, [dailyQueries.data]);

  const refresh = async () => {
    setIsRefreshing(true);
    try {
      await Promise.all([
        qc.refetchQueries({ queryKey: QK.indexQuotes }),
        hasMinuteCap && qc.refetchQueries({
          queryKey: QK.indexMinuteBatch(CORE_INDICES.map((i) => i.symbol)),
        }),
        qc.refetchQueries({ queryKey: ["indexDailyBatch"] }),
      ].filter(Boolean));
    } finally {
      setIsRefreshing(false);
    }
  };

  const handlePeriodChange = (symbol: string, period: PeriodType) => {
    setPeriods((prev) => ({ ...prev, [symbol]: period }));
  };

  return (
    <div className="h-full overflow-auto bg-base p-4">
      <div className="mb-4 flex items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-foreground">实时指数</h1>
          <p className="mt-1 text-xs text-muted">
            四大核心指数实时分时走势一览
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={refresh}
            disabled={isRefreshing || quotes.isPending || batchMinute.isPending || dailyQueries.isPending}
            className="inline-flex items-center gap-1.5 rounded-btn bg-accent px-3 py-1.5 text-xs font-medium text-white hover:bg-accent/90 disabled:opacity-50"
          >
            {isRefreshing || quotes.isPending || batchMinute.isPending || dailyQueries.isPending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <RefreshCw className="h-3.5 w-3.5" />
            )}
            {isRefreshing ? "刷新中…" : "刷新数据"}
          </button>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {CORE_INDICES.map((index) => (
          <IndexCard
            key={index.symbol}
            symbol={index.symbol}
            name={index.name}
            quote={quoteBySymbol.get(index.symbol)}
            minuteData={minuteBySymbol.get(index.symbol)}
            dailyData={dailyBySymbol.get(index.symbol)}
            minuteLoading={batchMinute.isPending}
            dailyLoading={dailyQueries.isPending}
            hasMinuteCap={hasMinuteCap}
            period={periods[index.symbol] || "1"}
            onPeriodChange={(p) => handlePeriodChange(index.symbol, p)}
          />
        ))}
      </div>
    </div>
  );
}
