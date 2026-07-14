import { describe, expect, it } from "vitest";
import { kindClass, sideClass } from "./colors";

describe("kindClass", () => {
  it("maps short-sleeve position lifecycle kinds to fill", () => {
    expect(kindClass("short_position_opened")).toBe("k-fill");
    expect(kindClass("short_position_closed")).toBe("k-fill");
  });

  it("maps insider-sleeve position lifecycle kinds to fill", () => {
    expect(kindClass("insider_position_opened")).toBe("k-fill");
    expect(kindClass("insider_position_closed")).toBe("k-fill");
  });

  it("maps short-sleeve run kinds to batch", () => {
    expect(kindClass("short_trade_run")).toBe("k-batch");
    expect(kindClass("short_drain_run")).toBe("k-batch");
    expect(kindClass("short_vetting_run")).toBe("k-batch");
  });

  it("maps insider-sleeve scan/run kinds to batch", () => {
    expect(kindClass("insider_scan_run")).toBe("k-batch");
    expect(kindClass("insider_trade_run")).toBe("k-batch");
  });

  it("maps short-sleeve error kinds to error", () => {
    expect(kindClass("short_trade_error")).toBe("k-error");
    expect(kindClass("short_drain_error")).toBe("k-error");
    expect(kindClass("short_vetting_error")).toBe("k-error");
  });

  it("maps insider-sleeve error kinds to error", () => {
    expect(kindClass("insider_scan_error")).toBe("k-error");
    expect(kindClass("insider_trade_error")).toBe("k-error");
    expect(kindClass("insider_memo_error")).toBe("k-error");
  });

  it("falls back to muted for an unknown kind", () => {
    expect(kindClass("some_totally_unrecognized_kind")).toBe("k-muted");
  });

  it("falls back to error for an unmapped kind ending in _error", () => {
    expect(kindClass("research_vetting_error")).toBe("k-error");
  });

  it("falls back to error for an unmapped kind ending in _failure", () => {
    expect(kindClass("baseline_quote_failure")).toBe("k-error");
  });

  it("falls back to muted for an unmapped kind with an unrelated suffix", () => {
    expect(kindClass("daily_overview")).toBe("k-muted");
  });
});

describe("sideClass", () => {
  it("maps known sides, falling back to other", () => {
    expect(sideClass("buy")).toBe("side-buy");
    expect(sideClass("short")).toBe("side-short");
    expect(sideClass("weird")).toBe("side-other");
  });
});
