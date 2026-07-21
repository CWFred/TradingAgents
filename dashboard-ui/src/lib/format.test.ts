import { describe, expect, it } from "vitest";
import { fmt2, fmtMoney, fmtPct, fmtQty, guardAge, relAge } from "./format";

describe("fmtMoney (string decimal, never floats)", () => {
  it("rounds half-up and groups thousands", () => {
    expect(fmtMoney("10450.2347", 2)).toBe("$10,450.23");
    expect(fmtMoney("10450.2379", 2)).toBe("$10,450.24");
    expect(fmtMoney("1234567.5", 0)).toBe("$1,234,568");
  });
  it("carries rounding across digits", () => {
    expect(fmtMoney("0.999", 2)).toBe("$1.00");
    expect(fmtMoney("9.995", 2)).toBe("$10.00");
  });
  it("handles negatives, null, and junk", () => {
    expect(fmtMoney("-5.005", 2)).toBe("−$5.01");
    expect(fmtMoney(null, 2)).toBe("—");
    expect(fmtMoney("not-a-number", 2)).toBe("not-a-number");
  });
  it("suppresses the sign when rounding collapses a negative to zero", () => {
    expect(fmtMoney("-0.001", 2)).toBe("$0.00");
    expect(fmtMoney("-0.4", 0)).toBe("$0");
    // a real negative still signs
    expect(fmtMoney("-0.006", 2)).toBe("−$0.01");
  });
  it("survives values beyond float precision", () => {
    expect(fmtMoney("90071992547409929.05", 2)).toBe("$90,071,992,547,409,929.05");
  });
});

describe("fmtPct", () => {
  it("scales ratio to percent with sign and class", () => {
    expect(fmtPct("0.0182")).toEqual({ text: "+1.82%", cls: "pos" });
    expect(fmtPct("-0.0064")).toEqual({ text: "−0.64%", cls: "neg" });
    expect(fmtPct("0")).toEqual({ text: "0.00%", cls: "flat" });
    expect(fmtPct(null)).toEqual({ text: "—", cls: "flat" });
  });
});

describe("fmtQty", () => {
  it("strips paper-fill precision noise", () => {
    expect(fmtQty("12.0000")).toBe("12");
    expect(fmtQty("0.5000")).toBe("0.5");
    expect(fmtQty("-30")).toBe("-30");
  });
  it("rounds long quantities to 2dp", () => {
    expect(fmtQty("12.3456789")).toBe("12.35");
    expect(fmtQty("100.005")).toBe("100.01");
    expect(fmtQty("7")).toBe("7");
    expect(fmtQty("-30.128")).toBe("-30.13");
    expect(fmtQty("5000")).toBe("5000");
    expect(fmtQty("12345.678")).toBe("12345.68");
  });
});

describe("fmt2 (bare 2dp number)", () => {
  it("formats decimal strings to 2dp without a currency symbol", () => {
    expect(fmt2("142.3")).toBe("142.30");
    expect(fmt2("12.3456")).toBe("12.35");
    expect(fmt2("-5.005")).toBe("−5.01");
    expect(fmt2(null)).toBe("—");
    expect(fmt2("")).toBe("—");
  });
});

describe("ages", () => {
  const now = Date.parse("2026-07-14T12:00:00Z");
  it("relAge buckets", () => {
    expect(relAge("2026-07-14T11:59:40Z", now)).toBe("just now");
    expect(relAge("2026-07-14T11:30:00Z", now)).toBe("30m ago");
    expect(relAge("2026-07-14T04:00:00Z", now)).toBe("8h ago");
    expect(relAge("2026-07-10T12:00:00Z", now)).toBe("4d ago");
    expect(relAge(null, now)).toBe("—");
  });
  it("guardAge buckets", () => {
    expect(guardAge(42)).toBe("42s");
    expect(guardAge(360)).toBe("6m");
    expect(guardAge(7300)).toBe("2h");
    expect(guardAge(null)).toBe("—");
  });
});
