import { useState } from "react";
import type { Section, Sleeve } from "../data/types";
import { SLEEVE_ORDER, isErr } from "../data/types";
import { fmtQty } from "../lib/format";
import Unavail from "./Unavail";

type SleevesSection = Section<Record<string, Section<Sleeve>>> | null;

function Group({ name, sleeve, open, onToggle, short }: {
  name: string; sleeve: Section<Sleeve>; open: boolean; onToggle: () => void;
  // short broker journals positive magnitudes, so short exposure is flagged by sleeve, not by sign
  short: boolean;
}) {
  const err = isErr(sleeve);
  const rows = err ? [] : sleeve.positions;
  const summary = err ? "unavailable"
    : `${rows.length} position${rows.length === 1 ? "" : "s"}`;
  return (
    <div className="acc-row">
      <button type="button" className="acc-head" onClick={onToggle}>
        <span className="l">
          <span className={`caret ${open ? "open" : ""}`}>▸</span>
          <span className="nm">{name}</span>
        </span>
        <span className="sum">{summary}</span>
      </button>
      {open && (
        <div className="acc-body">
          {err ? <Unavail msg={sleeve.error} />
            : rows.length === 0 ? <div className="acc-none">no open positions</div> : (
            <table className="tbl">
              <thead><tr>
                <th>symbol</th><th className="num">qty</th>
                <th className="num">entry</th><th className="num">stop</th>
              </tr></thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.symbol}>
                    <td className="sym">{r.symbol}</td>
                    <td className={`num ${short || r.quantity.startsWith("-") ? "neg" : ""}`}>
                      {fmtQty(r.quantity)}
                    </td>
                    <td className="num">{r.entry ?? "—"}</td>
                    <td className="num" style={{ color: "var(--tx3)" }}>{r.stop ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}

export default function PositionsPanel({ sleeves }: { sleeves: SleevesSection }) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({ momentum: true });
  const err = sleeves && isErr(sleeves) ? sleeves : null;
  // TS can't narrow `sleeves?.[name]` on the union — narrow once here.
  const data = sleeves && !isErr(sleeves) ? sleeves : null;
  const total = !data ? 0
    : Object.values(data).reduce(
        (n, s) => n + (isErr(s) ? 0 : s.positions.length), 0);
  return (
    <div className="panel">
      <div className="panel-head">
        <span>Positions</span>
        <span className="r">{total} open</span>
      </div>
      {err ? <Unavail msg={err.error} /> : (
        SLEEVE_ORDER.map((name) => {
          const s = data?.[name];
          if (!s) return null;
          return (
            <Group key={name} name={name} sleeve={s} open={!!expanded[name]}
              onToggle={() => setExpanded((e) => ({ ...e, [name]: !e[name] }))}
              short={name === "short"} />
          );
        })
      )}
    </div>
  );
}
