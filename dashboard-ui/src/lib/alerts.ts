import type { Health } from "../data/types";

export interface AlertView { tag: "ALERT" | "NOTICE"; conditions: string[] }

export function deriveAlert(health: Health | null): AlertView | null {
  if (!health) return null;
  const alerts: string[] = [];
  if (health.verdict === "STOPPED") alerts.push("service STOPPED — not trading");
  if (health.halts.daily_halt_today) alerts.push("daily drawdown halt in effect");
  if (health.halts.kill_switch_this_week) alerts.push("weekly kill-switch tripped");
  const notices: string[] = [];
  if (health.verdict === "STALE") notices.push("guardian heartbeat is stale");
  if (health.research_paused) notices.push("TradingAgents model work paused");
  if (alerts.length) return { tag: "ALERT", conditions: [...alerts, ...notices] };
  if (notices.length) return { tag: "NOTICE", conditions: notices };
  return null;
}
