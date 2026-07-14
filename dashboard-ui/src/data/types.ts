export interface SectionError { error: string }
export type Section<T> = T | SectionError;

export function isErr(x: unknown): x is SectionError {
  return typeof x === "object" && x !== null && "error" in x;
}

export const SLEEVE_ORDER = ["momentum", "research", "baseline", "short", "insider"] as const;
export type SleeveName = (typeof SLEEVE_ORDER)[number];

export interface Health {
  verdict: "RUNNING" | "STALE" | "STOPPED" | "UNKNOWN";
  broker_mode: string;
  guardian: { alive_at: string | null; age_seconds: number | null };
  halts: { daily_halt_today: boolean; kill_switch_this_week: boolean };
  research_paused: boolean;
}

export interface Market {
  is_open: boolean;
  next_open: string | null;
  previous_close: string | null;
}

export interface Position { symbol: string; quantity: string; entry: string | null; stop: string | null }
export interface Fill { symbol: string; side: string; quantity: string; price: string; filled_at: string }
export interface SeriesPoint { at: string; equity: string }

export interface Sleeve {
  equity: string | null;
  cash: string | null;
  day_pnl_pct: string | null;
  lifetime_pnl_pct: string | null;
  series: SeriesPoint[];
  positions: Position[];
  fills_today: Fill[];
}

export interface MemoRow {
  memo_id: string; ticker: string; thesis_type: string;
  conviction_tier: number | string; created_at: string; status: string;
}

export interface EventView { at: string; age_seconds: number }

export interface Funnel {
  screener: {
    last_run: { asof: string; universe_size: number; passed_count: number } | null;
    hits_by_status: Record<string, number>;
  };
  memos: { by_status: Record<string, number>; open: MemoRow[] };
  overnight: {
    last_vetting_run: EventView | null;
    last_drain_run: EventView | null;
    paused: boolean;
  };
  signals_7d: Record<string, number>;
}

export interface AnomalyEntry { count: number; last_at: string | null }

export interface Snapshot {
  generated_at: string;
  health: Section<Health>;
  sleeves: Section<Record<string, Section<Sleeve>>>;
  funnel: Section<Funnel>;
  anomalies_7d: Section<Record<string, AnomalyEntry>>;
  market: Section<Market>;
}

export interface EventItem {
  source: string; id: number; at: string; kind: string; text: string;
}
