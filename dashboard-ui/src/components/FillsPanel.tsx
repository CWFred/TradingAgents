import type { Fill, Section, Sleeve } from "../data/types";
import { SLEEVE_ORDER, isErr } from "../data/types";
import { fmtQty, hhmmss } from "../lib/format";
import { sideClass } from "../lib/colors";
import Unavail from "./Unavail";

type SleevesSection = Section<Record<string, Section<Sleeve>>> | null;

export default function FillsPanel({ sleeves }: { sleeves: SleevesSection }) {
  const err = sleeves && isErr(sleeves) ? sleeves : null;
  // TS can't narrow `sleeves[name]` on the union — narrow once here.
  const data = sleeves && !isErr(sleeves) ? sleeves : null;
  const fills: (Fill & { sleeve: string })[] = [];
  if (data) {
    for (const name of SLEEVE_ORDER) {
      const s = data[name];
      if (s && !isErr(s)) for (const f of s.fills_today) fills.push({ ...f, sleeve: name });
    }
    fills.sort((a, b) => (a.filled_at < b.filled_at ? -1 : a.filled_at > b.filled_at ? 1 : 0));
  }
  return (
    <div className="panel">
      <div className="panel-head">
        <span>Fills today</span>
        <span className="r">{fills.length}</span>
      </div>
      {err ? <Unavail msg={err.error} />
        : fills.length === 0 ? <div className="panel-empty">no fills today</div> : (
        <div style={{ maxHeight: 260, overflow: "auto" }}>
          <table className="tbl padded">
            <thead className="sticky"><tr>
              <th>time</th><th>sleeve</th><th>side</th><th>symbol</th>
              <th className="num">qty</th><th className="num">price</th>
            </tr></thead>
            <tbody>
              {fills.map((f, i) => (
                <tr key={`${f.sleeve}-${f.filled_at}-${i}`}>
                  <td>{hhmmss(f.filled_at).slice(0, 5)}</td>
                  <td style={{ fontFamily: "var(--sans)", textTransform: "capitalize" }}>{f.sleeve}</td>
                  <td><span className={`badge ${sideClass(f.side)}`}>{f.side.toUpperCase()}</span></td>
                  <td className="sym">{f.symbol}</td>
                  <td className="num">{fmtQty(f.quantity)}</td>
                  <td className="num" style={{ color: "var(--tx)" }}>{f.price}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
