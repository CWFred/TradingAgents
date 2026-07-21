// @vitest-environment jsdom
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { usePnl, usePnlMode } from "./pnl";
import * as api from "./api";

afterEach(() => { vi.restoreAllMocks(); localStorage.clear(); });

describe("usePnl", () => {
  it("fetches when active and maps rows by symbol", async () => {
    vi.spyOn(api, "fetchPnl").mockResolvedValue({
      sleeve: "momentum",
      positions: [{ symbol: "BAH", price: "110", pnl_dollar: "100", pnl_pct: "0.1" }],
    });
    const { result } = renderHook(() => usePnl("momentum", true, 999999));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.rows.BAH.pnl_dollar).toBe("100");
  });

  it("does not fetch when inactive", () => {
    const spy = vi.spyOn(api, "fetchPnl").mockResolvedValue({ sleeve: "x", positions: [] });
    renderHook(() => usePnl("momentum", false, 999999));
    expect(spy).not.toHaveBeenCalled();
  });
});

describe("usePnlMode", () => {
  it("defaults to pct and toggles, persisting to localStorage", () => {
    const { result } = renderHook(() => usePnlMode());
    expect(result.current[0]).toBe("pct");
    act(() => result.current[1]());
    expect(result.current[0]).toBe("dollar");
    expect(localStorage.getItem("pnlMode")).toBe("dollar");
  });
});
