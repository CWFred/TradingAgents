import { describe, expect, it } from "vitest";
import { deriveAlert } from "./alerts";
import type { Health } from "../data/types";

const base: Health = {
  verdict: "RUNNING", broker_mode: "paper",
  guardian: { alive_at: "t", age_seconds: 30 },
  halts: { daily_halt_today: false, kill_switch_this_week: false },
  research_paused: false,
};

describe("deriveAlert", () => {
  it("healthy → no banner", () => {
    expect(deriveAlert(base)).toBeNull();
    expect(deriveAlert(null)).toBeNull();
  });
  it("STOPPED and halts → ALERT with every condition", () => {
    const a = deriveAlert({ ...base, verdict: "STOPPED",
      halts: { daily_halt_today: true, kill_switch_this_week: true } })!;
    expect(a.tag).toBe("ALERT");
    expect(a.conditions).toHaveLength(3);
  });
  it("halt alone still ALERTs while RUNNING", () => {
    const a = deriveAlert({ ...base, halts: { ...base.halts, daily_halt_today: true } })!;
    expect(a.tag).toBe("ALERT");
    expect(a.conditions).toEqual(["daily drawdown halt in effect"]);
  });
  it("STALE / paused → NOTICE", () => {
    expect(deriveAlert({ ...base, verdict: "STALE" })!.tag).toBe("NOTICE");
    expect(deriveAlert({ ...base, research_paused: true })!.conditions)
      .toEqual(["TradingAgents model work paused"]);
  });
});
