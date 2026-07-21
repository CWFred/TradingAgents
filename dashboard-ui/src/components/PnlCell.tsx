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
  if (!row || (row.pnl_dollar == null && row.pnl_pct == null)) {
    return <td className="num" style={{ color: "var(--tx3)" }}>…</td>;
  }
  if (mode === "dollar") {
    const v = row.pnl_dollar;
    const neg = v != null && v.startsWith("-");
    const cls = v == null ? "" : neg ? "neg" : /[1-9]/.test(v) ? "pos" : "flat";
    // fmtMoney handles the sign; strip its leading "-" source since it signs itself
    return <td className={`num ${cls}`}>{fmtMoney(v, 2)}</td>;
  }
  const p = fmtPct(row.pnl_pct);
  return <td className={`num ${p.cls}`}>{p.text}</td>;
}
