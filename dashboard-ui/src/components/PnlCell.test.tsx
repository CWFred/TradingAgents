// @vitest-environment jsdom
import { render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { PnlCell } from "./PnlCell";
import type { PnlRow } from "../data/types";

function cell(row: PnlRow | undefined) {
  // A <td> must live inside a table row to render without a DOM warning.
  const { container } = render(
    <table><tbody><tr><PnlCell row={row} mode="dollar" /></tr></tbody></table>,
  );
  return container.querySelector("td")!;
}

afterEach(() => document.body.replaceChildren());

describe("PnlCell", () => {
  it("shows the loading placeholder when the row is not fetched yet", () => {
    expect(cell(undefined).textContent).toBe("…");
  });

  it("shows an em dash (not the loading placeholder) on a per-symbol quote error", () => {
    const td = cell({ symbol: "BAH", price: null, pnl_dollar: null, pnl_pct: null,
                      error: "yfinance quote fetch for BAH failed" });
    expect(td.textContent).toBe("—");
    expect(td.getAttribute("title")).toBe("yfinance quote fetch for BAH failed");
  });

  it("renders the dollar P&L when present", () => {
    const td = cell({ symbol: "BAH", price: "110", pnl_dollar: "100", pnl_pct: "0.1" });
    expect(td.textContent).not.toBe("…");
    expect(td.textContent).not.toBe("—");
    expect(td.className).toContain("pos");
  });
});
