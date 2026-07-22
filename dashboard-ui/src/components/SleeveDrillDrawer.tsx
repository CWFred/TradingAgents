import { useEffect } from "react";
import type { Section, Sleeve } from "../data/types";
import { isErr } from "../data/types";
import { fmt2, fmtMoney, fmtPct, fmtQty, fmtSignedMoney, hhmmss } from "../lib/format";
import { sideClass } from "../lib/colors";
import { usePnl, usePnlMode } from "../data/pnl";
import Sparkline from "./Sparkline";
import { PnlCell, PnlHeader } from "./PnlCell";
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

  const [mode, toggleMode] = usePnlMode();
  const active = !!sleeve && !isErr(sleeve);
  const { rows: pnl } = usePnl(name, active);

  const body = () => {
    if (!sleeve) return <div className="panel-empty">no data</div>;
    if (isErr(sleeve)) return <Unavail msg={sleeve.error} />;
    const shortSleeve = name === "short"; // short broker journals positive magnitudes — flag exposure by sleeve, not sign
    const day = fmtPct(sleeve.day_pnl_pct);
    const life = fmtPct(sleeve.lifetime_pnl_pct);
    const unrealized = fmtSignedMoney(sleeve.unrealized_pnl, 2);
    const grossPct = fmtPct(sleeve.gross_short_exposure_pct).text.replace(/^\+/, "");
    return (
      <>
        {shortSleeve && <span className="drawer-eq-label">net equity</span>}
        <div className="drawer-eq">
          <span className="big" title={sleeve.equity ? `$${sleeve.equity}` : undefined}>
            {fmtMoney(sleeve.equity, 2)}
          </span>
          <span className={`day ${day.cls}`}>{day.text}</span>
        </div>
        {shortSleeve ? (
          <>
            <div className="short-drawer-metrics">
              <div className="short-metric">
                <span className="metric-label">total return</span>
                <span className={`metric-value ${life.cls}`}>{life.text}</span>
              </div>
              <div className="short-metric">
                <span className="metric-label">open P&amp;L</span>
                <span className={`metric-value ${unrealized.cls}`}>{unrealized.text}</span>
              </div>
              <div className="short-metric">
                <span className="metric-label">gross short exposure</span>
                <span className="metric-value">{fmtMoney(sleeve.gross_short_exposure, 2)}</span>
                <span className="metric-note">{grossPct} of net equity</span>
              </div>
              <div className="short-metric">
                <span className="metric-label">collateral cash</span>
                <span className="metric-value muted">{fmtMoney(sleeve.collateral_cash, 2)}</span>
                <span className="metric-note">includes borrowed-share proceeds</span>
              </div>
            </div>
            <div className="short-accounting-note">
              Collateral cash is not profit. Net equity subtracts the current cost to cover every open short.
            </div>
          </>
        ) : (
          <div className="drawer-sub">
            <span>lifetime <span className={life.cls}>{life.text}</span></span>
            <span>cash <span style={{ color: "var(--tx2)" }}>{fmtMoney(sleeve.cash, 2)}</span></span>
          </div>
        )}
        <Sparkline series={sleeve.series} w={700} h={120}
          up={day.cls !== "neg"} className="big" />

        <span className="mini-label">Positions</span>
        {sleeve.positions.length === 0
          ? <div className="none">no open positions</div> : (
          <table className="tbl" style={{ marginBottom: 20 }}>
            <thead><tr>
              <th>symbol</th><th className="num">qty</th>
              <th className="num">entry</th><th className="num">stop</th>
              <PnlHeader mode={mode} onToggle={toggleMode} />
            </tr></thead>
            <tbody>
              {sleeve.positions.map((p) => (
                <tr key={p.symbol}>
                  <td className="sym">{p.symbol}</td>
                  <td className={`num ${shortSleeve || p.quantity.startsWith("-") ? "neg" : ""}`}>{fmtQty(p.quantity)}</td>
                  <td className="num">{p.entry ? fmt2(p.entry) : "—"}</td>
                  <td className="num" style={{ color: "var(--tx3)" }}>{p.stop ? fmt2(p.stop) : "—"}</td>
                  <PnlCell row={pnl[p.symbol]} mode={mode} />
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
                  <td className="num" style={{ color: "var(--tx)" }}>{fmt2(f.price)}</td>
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
