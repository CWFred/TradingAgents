import { fmtMoney, fmtPct } from "../lib/format";
import type { PnlMode } from "../data/pnl";
import type { PnlRow } from "../data/types";

export function PnlHeader({ mode, onToggle }: { mode: PnlMode; onToggle: () => void }) {
  return (
    <th className="num pnl-h">
      <button type="button" className="pnl-toggle" onClick={onToggle}
        title="toggle dollars / percent">
        P&L {mode === "dollar" ? "$" : "%"}
      </button>
    </th>
  );
}

export function PnlCell({ row, mode }: { row: PnlRow | undefined; mode: PnlMode }) {
  if (!row) {
    // Not fetched yet: loading placeholder.
    return <td className="num" style={{ color: "var(--tx3)" }}>…</td>;
  }
  if (row.pnl_dollar == null && row.pnl_pct == null) {
    // Fetched but unquotable (per-symbol quote error): dash, reason on hover.
    return <td className="num" style={{ color: "var(--tx3)" }} title={row.error}>—</td>;
  }
  if (mode === "dollar") {
    const v = row.pnl_dollar;
    const neg = v != null && v.startsWith("-");
    const cls = v == null ? "" : neg ? "neg" : /[1-9]/.test(v) ? "pos" : "flat";
    return <td className={`num ${cls}`}>{fmtMoney(v, 2)}</td>;
  }
  const p = fmtPct(row.pnl_pct);
  return <td className={`num ${p.cls}`}>{p.text}</td>;
}
