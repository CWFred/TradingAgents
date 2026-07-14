import { useEffect } from "react";
import type { Section, Sleeve } from "../data/types";
import { isErr } from "../data/types";
import { fmtMoney, fmtPct, fmtQty, hhmmss } from "../lib/format";
import { sideClass } from "../lib/colors";
import Sparkline from "./Sparkline";
import Unavail from "./Unavail";

const KIND_LABELS: Record<string, string> = {
  momentum: "intraday momentum",
  research: "LLM long theses",
  baseline: "passive benchmark",
  short: "short-selling",
  insider: "Form-4 clusters",
};

export default function SleeveDrillDrawer({ name, sleeve, onClose }: {
  name: string; sleeve: Section<Sleeve> | undefined; onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const body = () => {
    if (!sleeve) return <div className="panel-empty">no data</div>;
    if (isErr(sleeve)) return <Unavail msg={sleeve.error} />;
    const shortSleeve = name === "short"; // short broker journals positive magnitudes — flag exposure by sleeve, not sign
    const day = fmtPct(sleeve.day_pnl_pct);
    const life = fmtPct(sleeve.lifetime_pnl_pct);
    return (
      <>
        <div className="drawer-eq">
          <span className="big" title={sleeve.equity ? `$${sleeve.equity}` : undefined}>
            {fmtMoney(sleeve.equity, 2)}
          </span>
          <span className={`day ${day.cls}`}>{day.text}</span>
        </div>
        <div className="drawer-sub">
          <span>lifetime <span className={life.cls}>{life.text}</span></span>
          <span>cash <span style={{ color: "var(--tx2)" }}>{fmtMoney(sleeve.cash, 2)}</span></span>
        </div>
        <Sparkline series={sleeve.series} w={520} h={120}
          up={day.cls !== "neg"} className="big" />

        <span className="mini-label">Positions</span>
        {sleeve.positions.length === 0
          ? <div className="none">no open positions</div> : (
          <table className="tbl" style={{ marginBottom: 20 }}>
            <thead><tr>
              <th>symbol</th><th className="num">qty</th>
              <th className="num">entry</th><th className="num">stop</th>
            </tr></thead>
            <tbody>
              {sleeve.positions.map((p) => (
                <tr key={p.symbol}>
                  <td className="sym">{p.symbol}</td>
                  <td className={`num ${shortSleeve || p.quantity.startsWith("-") ? "neg" : ""}`}>{fmtQty(p.quantity)}</td>
                  <td className="num">{p.entry ?? "—"}</td>
                  <td className="num" style={{ color: "var(--tx3)" }}>{p.stop ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        <span className="mini-label">Fills today</span>
        {sleeve.fills_today.length === 0
          ? <div className="none">no fills today</div> : (
          <table className="tbl">
            <tbody>
              {sleeve.fills_today.map((f, i) => (
                <tr key={`${f.filled_at}-${i}`}>
                  <td style={{ color: "var(--tx3)" }}>{hhmmss(f.filled_at).slice(0, 5)}</td>
                  <td><span className={`badge ${sideClass(f.side)}`}>{f.side.toUpperCase()}</span></td>
                  <td className="sym">{f.symbol}</td>
                  <td className="num">{fmtQty(f.quantity)}</td>
                  <td className="num" style={{ color: "var(--tx)" }}>{f.price}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </>
    );
  };

  return (
    <>
      <button type="button" className="overlay" onClick={onClose} aria-label="close" />
      <div className="drawer" role="dialog" aria-label={`${name} sleeve detail`}>
        <div className="drawer-head">
          <span>
            <span className="nm">{name}</span>
            <span className="kind">{KIND_LABELS[name] ?? ""}</span>
          </span>
          <button type="button" className="drawer-x" onClick={onClose}>✕</button>
        </div>
        <div className="drawer-body">{body()}</div>
      </div>
    </>
  );
}
