import { describe, expect, it } from "vitest";
import { DISCONNECT_AFTER, initialPollState, isDisconnected, pollReducer } from "./poll";
import type { Snapshot } from "./types";

const snap = { generated_at: "t" } as Snapshot;

describe("pollReducer", () => {
  it("success stores data and resets failures", () => {
    const failed = pollReducer(initialPollState, { type: "failure" });
    const s = pollReducer(failed, { type: "success", snapshot: snap, events: [], at: 123 });
    expect(s.snapshot).toBe(snap);
    expect(s.failures).toBe(0);
    expect(s.lastGoodAt).toBe(123);
  });

  it("failure keeps last-good data while counting up", () => {
    let s = pollReducer(initialPollState, { type: "success", snapshot: snap, events: [], at: 1 });
    s = pollReducer(s, { type: "failure" });
    expect(s.snapshot).toBe(snap);
    expect(s.failures).toBe(1);
    expect(isDisconnected(s)).toBe(false);
  });

  it(`disconnects after ${DISCONNECT_AFTER} consecutive failures`, () => {
    let s = pollReducer(initialPollState, { type: "success", snapshot: snap, events: [], at: 1 });
    for (let i = 0; i < DISCONNECT_AFTER; i++) s = pollReducer(s, { type: "failure" });
    expect(isDisconnected(s)).toBe(true);
    s = pollReducer(s, { type: "success", snapshot: snap, events: [], at: 2 });
    expect(isDisconnected(s)).toBe(false);
  });
});
