import type { Section, Sleeve } from "../data/types";
import { SLEEVE_ORDER, isErr } from "../data/types";
import { fmtMoney, fmtPct, fmtSignedMoney } from "../lib/format";
import Sparkline from "./Sparkline";
import Unavail from "./Unavail";

type SleevesSection = Section<Record<string, Section<Sleeve>>> | null;

// Header label only: floats never touch a displayed per-sleeve money value.
export function totalEquityLabel(sleeves: SleevesSection): string {
  if (!sleeves || isErr(sleeves)) return "—";
  let total = 0;
  let any = false;
  for (const s of Object.values(sleeves)) {
    if (!isErr(s) && s.equity != null) { total += Number(s.equity); any = true; }
  }
  return any ? fmtMoney(total.toFixed(2), 2) : "—";
}

function Card({ name, sleeve, onOpen }: {
  name: string; sleeve: Section<Sleeve>; onOpen: () => void;
}) {
  if (isErr(sleeve)) {
    return (
      <button type="button" className="card" onClick={onOpen}>
        <div className="card-top"><span className="nm">{name}</span></div>
        <Unavail msg={sleeve.error} />
      </button>
    );
  }
  const shortSleeve = name === "short";
  const day = fmtPct(sleeve.day_pnl_pct);
  const life = fmtPct(sleeve.lifetime_pnl_pct);
  const unrealized = fmtSignedMoney(sleeve.unrealized_pnl, 2);
  const grossPct = fmtPct(sleeve.gross_short_exposure_pct).text.replace(/^\+/, "");
  const up = day.cls !== "neg";
  return (
    <button type="button" className={`card${shortSleeve ? " card-short" : ""}`} onClick={onOpen}>
      <div className="card-top">
        <span className="nm">{name}</span>
        <span className={`day ${day.cls}`}>{shortSleeve ? "today " : ""}{day.text}</span>
      </div>
      {shortSleeve && <div className="metric-label">net equity</div>}
      <div className="card-eq" title={sleeve.equity ? `$${sleeve.equity}` : undefined}>
        {fmtMoney(sleeve.equity, 2)}
      </div>
      <Sparkline series={sleeve.series} w={shortSleeve ? 260 : 120} h={30} up={up} />
      {shortSleeve ? (
        <div className="short-card-metrics">
          <div className="short-metric">
            <span className="metric-label">total return</span>
            <span className={`metric-value ${life.cls}`}>{life.text}</span>
          </div>
          <div className="short-metric">
            <span className="metric-label">open P&amp;L</span>
            <span className={`metric-value ${unrealized.cls}`}>{unrealized.text}</span>
          </div>
          <div className="short-metric">
            <span className="metric-label">gross short</span>
            <span className="metric-value">{fmtMoney(sleeve.gross_short_exposure, 0)}</span>
            <span className="metric-note">{grossPct} of equity</span>
          </div>
          <div className="short-metric">
            <span className="metric-label">collateral cash</span>
            <span className="metric-value muted">{fmtMoney(sleeve.collateral_cash, 0)}</span>
            <span className="metric-note">includes short proceeds</span>
          </div>
        </div>
      ) : (
        <div className="card-foot">
          <span>life <span className={life.cls}>{life.text}</span></span>
          <span>cash {fmtMoney(sleeve.cash, 2)}</span>
        </div>
      )}
    </button>
  );
}

export default function SleeveCards({ sleeves, onOpen }: {
  sleeves: SleevesSection; onOpen: (name: string) => void;
}) {
  // TS can't narrow `sleeves?.[name]` on the union — narrow once here.
  const data = sleeves && !isErr(sleeves) ? sleeves : null;
  return (
    <>
      <div className="sec-head">
        <span className="t">Sleeves</span>
        <span className="r">total {totalEquityLabel(sleeves)}</span>
      </div>
      {sleeves && isErr(sleeves) ? (
        <div className="panel" style={{ marginBottom: 18 }}><Unavail msg={sleeves.error} /></div>
      ) : (
        <div className="sleeves">
          {SLEEVE_ORDER.map((name) => {
            const s = data?.[name];
            if (!s) return null;
            return <Card key={name} name={name} sleeve={s} onOpen={() => onOpen(name)} />;
          })}
        </div>
      )}
    </>
  );
}
