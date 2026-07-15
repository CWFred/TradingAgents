import { describe, expect, it } from "vitest";
import type { Activity } from "../data/types";
import { fmtDur, nowLine, runOutcome } from "./activity";

const base: Activity = { current: null, stale: false, recent_runs: [], next_work: [] };

describe("fmtDur", () => {
  it("formats", () => {
    expect(fmtDur(null)).toBe("—");
    expect(fmtDur(42)).toBe("42s");
    expect(fmtDur(720)).toBe("12m");
    expect(fmtDur(7500)).toBe("2h 05m");
  });
});

describe("nowLine", () => {
  it("busy: item with seq", () => {
    const a: Activity = {
      ...base,
      current: {
        job: "daily_cycle", stage: "analyzing", symbol: "BAH", seq: "3",
        reason: null, started_at: "2026-07-14T16:40:00+00:00", age_seconds: 360,
      },
    };
    const line = nowLine(a, "RUNNING");
    expect(line.state).toBe("busy");
    expect(line.text).toBe("daily cycle — analyzing BAH (3)");
  });

  it("busy: job-level fallback shows reason", () => {
    const a: Activity = {
      ...base,
      current: {
        job: "overnight", stage: null, symbol: null, seq: null,
        reason: "2 hit(s) to research", started_at: "2026-07-14T04:00:00+00:00",
        age_seconds: 60,
      },
    };
    expect(nowLine(a, "RUNNING").text).toBe("overnight — 2 hit(s) to research");
  });

  it("idle: shows next work headline", () => {
    const a: Activity = {
      ...base,
      next_work: [{ at: "2026-07-15T04:00:00+00:00", job: "overnight",
                    purpose: "screen due · 2 hit(s) to research" }],
    };
    const line = nowLine(a, "RUNNING");
    expect(line.state).toBe("idle");
    expect(line.text).toContain("overnight");
    expect(line.text).toContain("screen due · 2 hit(s) to research");
  });

  it("stale wins", () => {
    expect(nowLine({ ...base, stale: true }, "STOPPED").state).toBe("stale");
  });

  it("null activity is unknown", () => {
    expect(nowLine(null, "UNKNOWN").state).toBe("unknown");
  });
});

describe("runOutcome", () => {
  const run = {
    job: "overnight", reason: null, started_at: "x", finished_at: null,
    ok: null, duration_s: null, outcome: null,
  };
  it("open run", () => expect(runOutcome(run)).toBe("running…"));
  it("finished", () =>
    expect(runOutcome({ ...run, finished_at: "y", ok: true,
                        outcome: "researched 4" })).toBe("researched 4"));
  it("failed without outcome", () =>
    expect(runOutcome({ ...run, finished_at: "y", ok: false })).toBe("failed"));
});
