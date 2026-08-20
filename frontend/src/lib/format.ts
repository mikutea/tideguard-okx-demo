import type { LongRunStatus, MarketData } from "../types";

export function numberValue(value: string | number | null | undefined): number | null {
  if (value === "" || value === null || value === undefined) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function formatNumber(value: string | number | null | undefined, digits = 2): string {
  const parsed = numberValue(value);
  if (parsed === null) return "—";
  return new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits
  }).format(parsed);
}

export function formatCompact(value: string | number | null | undefined): string {
  const parsed = numberValue(value);
  if (parsed === null) return "—";
  return new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 2 }).format(parsed);
}

export function formatPercent(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${value >= 0 ? "+" : ""}${(value * 100).toFixed(digits)}%`;
}

export function formatTime(value: string | number | null | undefined, includeDate = true): string {
  if (value === null || value === undefined || value === "") return "—";
  const date = typeof value === "number" || /^\d+$/.test(String(value))
    ? new Date(Number(value))
    : new Date(String(value));
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    ...(includeDate ? { month: "2-digit", day: "2-digit" } : {}),
    hour: "2-digit",
    minute: "2-digit",
    second: includeDate ? undefined : "2-digit",
    hour12: false
  }).format(date);
}

export function ageSeconds(timestamp?: string | null): number | null {
  if (!timestamp) return null;
  const age = (Date.now() - Number(timestamp)) / 1000;
  return Number.isFinite(age) ? Math.max(0, age) : null;
}

export interface PerformancePoint {
  ts: number;
  label: string;
  strategy: number;
  benchmark: number;
  drawdown: number;
}

export function buildPerformanceSeries(
  market: MarketData | null,
  positions: LongRunStatus["recentPositions"]
): PerformancePoint[] {
  const candles = market?.candles ?? [];
  if (candles.length < 2) return [];
  const first = numberValue(candles[0].close);
  if (!first || first <= 0) return [];

  const settlements = positions
    .filter((position) => position.closedAt && position.realizedReturn !== null)
    .map((position) => ({ ts: new Date(position.closedAt!).getTime(), value: position.realizedReturn! }))
    .filter((position) => Number.isFinite(position.ts))
    .sort((a, b) => a.ts - b.ts);

  let settlementIndex = 0;
  let equity = 1;
  let peak = 1;
  return candles.map((candle) => {
    const ts = Number(candle.ts);
    while (settlementIndex < settlements.length && settlements[settlementIndex].ts <= ts) {
      equity *= 1 + settlements[settlementIndex].value;
      peak = Math.max(peak, equity);
      settlementIndex += 1;
    }
    const close = numberValue(candle.close) ?? first;
    return {
      ts,
      label: formatTime(ts),
      strategy: (equity - 1) * 100,
      benchmark: ((close / first) - 1) * 100,
      drawdown: peak > 0 ? ((equity / peak) - 1) * 100 : 0
    };
  });
}

export function shortId(value: string | null | undefined, left = 8): string {
  if (!value) return "—";
  if (value.length <= left + 4) return value;
  return `${value.slice(0, left)}…${value.slice(-4)}`;
}
