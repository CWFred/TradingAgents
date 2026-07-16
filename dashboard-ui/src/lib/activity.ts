import type { Activity } from "../data/types";
import { hhmmET } from "./format";

// "daily_cycle" -> "daily cycle" for display.
const jobLabel = (job: string) => job.replace(/_/g, " ");

export function fmtDur(s: number | null | undefined): string {
  if (s == null) return "—";
  if (s < 60) return `${Math.round(s)}s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${String(m % 60).padStart(2, "0")}m`;
}

export interface NowLineResult {
  state: "busy" | "idle" | "stale" | "unknown";
  text: string;
}

export function nowLine(
  a: Activity | null, healthVerdict: string,
): NowLineResult {
  if (a == null) return { state: "unknown", text: "activity unavailable" };
  if (a.stale) {
    return { state: "stale",
             text: `activity trail went cold (service ${healthVerdict})` };
  }
  const c = a.current;
  if (c != null) {
    let what: string;
    if (c.stage != null) {
      what = c.stage + (c.symbol ? ` ${c.symbol}` : "") + (c.seq ? ` (${c.seq})` : "");
    } else {
      what = c.reason ?? "working";
    }
    return { state: "busy", text: `${jobLabel(c.job)} — ${what}` };
  }
  const next = a.next_work[0];
  if (next != null) {
    return { state: "idle",
             text: `idle — next: ${jobLabel(next.job)} ${hhmmET(next.at)} — ${next.purpose}` };
  }
  return { state: "idle", text: "idle" };
}

export function runOutcome(r: {
  finished_at: string | null; ok: boolean | null; outcome: string | null;
}): string {
  if (r.finished_at == null && r.ok == null) return r.outcome ?? "running…";
  if (r.outcome != null) return r.outcome;
  return r.ok ? "done" : "failed";
}
