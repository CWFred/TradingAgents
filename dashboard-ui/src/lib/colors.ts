const KIND_GROUPS: Record<string, string> = {
  fill: "fill",
  research_position_opened: "fill",
  research_position_closed: "fill",
  stop_hit: "order",
  order_rejected: "order",
  analysis_decision: "signal",
  research_vetting_run: "batch",
  research_drain_run: "batch",
  baseline_screen_run: "batch",
  daily_cycle_run: "batch",
  daily_cycle_completed: "batch",
  falsifier_tripped: "error",
  daily_halt: "error",
  kill_switch: "error",
  stop_failed: "error",
  startup_halted: "error",
  inconsistency: "error",
  guardian_check_error: "error",
  heartbeat_error: "error",
  research_escalation: "memo",
  resolution_due: "memo",
  catalyst_due: "memo",
  service_started: "muted",
  service_stopping: "muted",

  // Short sleeve (mirrors the research sleeve's kinds — see
  // ops/events.py KIND_SHORT_*).
  short_trade_run: "batch",
  short_drain_run: "batch",
  short_vetting_run: "batch",
  short_trade_error: "error",
  short_drain_error: "error",
  short_vetting_error: "error",
  short_position_opened: "fill",
  short_position_closed: "fill",

  // Insider-cluster sleeve (see ops/events.py KIND_INSIDER_*).
  insider_scan_run: "batch",
  insider_trade_run: "batch",
  insider_scan_error: "error",
  insider_trade_error: "error",
  insider_memo_error: "error",
  insider_position_opened: "fill",
  insider_position_closed: "fill",

  // Other high-signal safety kinds (ops/events.py) that were missing
  // from this table — all are instant-critical or high-urgency in
  // ops/notify/policy.py's POLICY table (or the sibling of one).
  order_not_filled: "order",
  kill_switch_close_failed: "error",
  positions_recovered_without_stops: "error",
  guardian_blind: "error",
  universe_blind: "error",
};

// This table has drifted out of sync with ops/events.py's kind list twice
// on this branch (new sleeves add kinds here late, or not at all). Rather
// than trust the table to stay complete, fall back on the one naming
// convention backend kinds actually honor: anything ending in `_error` or
// `_failure` is an error-class event, mapped or not.
export const kindClass = (kind: string) => {
  const group = KIND_GROUPS[kind];
  if (group) return "k-" + group;
  return kind.endsWith("_error") || kind.endsWith("_failure") ? "k-error" : "k-muted";
};

const SIDES = new Set(["buy", "sell", "short", "cover"]);
export const sideClass = (side: string) => {
  const s = side.toLowerCase();
  return "side-" + (SIDES.has(s) ? s : "other");
};
