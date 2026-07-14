import type { Funnel, Section } from "../data/types";
import { isErr } from "../data/types";
import { relAge } from "../lib/format";
import Unavail from "./Unavail";

const MEMO_PILL_ORDER = ["open", "closed", "rejected"];
const MEMO_COLORS: Record<string, string> = {
  open: "var(--acc)", closed: "var(--pos)", rejected: "var(--tx3)",
};
const SIGNAL_LABELS: Record<string, string> = {
  falsifier_tripped: "falsifier", research_escalation: "escalation",
  resolution_due: "resolution due", catalyst_due: "catalyst due",
};

function tierBadge(tier: number | string): { label: string; color: string } {
  const n = String(tier).replace(/^t/i, "");
  const color = n === "1" ? "var(--acc)" : n === "2" ? "var(--tx2)" : "var(--tx3)";
  return { label: `T${n}`, color };
}

export default function FunnelPanel({ funnel }: { funnel: Section<Funnel> | null }) {
  const body = () => {
    if (!funnel) return <div className="panel-empty">waiting for snapshot…</div>;
    if (isErr(funnel)) return <Unavail msg={funnel.error} />;
    const run = funnel.screener.last_run;
    const byStatus = funnel.memos.by_status;
    const pillKeys = [
      ...MEMO_PILL_ORDER.filter((k) => k in byStatus),
      ...Object.keys(byStatus).filter((k) => !MEMO_PILL_ORDER.includes(k)).sort(),
    ];
    return (
      <div style={{ padding: "14px 16px", display: "flex", flexDirection: "column", gap: 13 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, color: "var(--tx2)" }}>
          <span style={{ color: "var(--tx3)" }}>screener last run</span>
          <span className="mono" style={{ color: "var(--tx)" }}>
            {run ? `${run.passed_count} / ${run.universe_size} · ${run.asof}` : "—"}
          </span>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
          <span className="mini-label">memos</span>
          <div className="pill-row">
            {pillKeys.length === 0 && <span className="pill">none</span>}
            {pillKeys.map((k) => (
              <span key={k} className="pill">
                <b style={{ color: MEMO_COLORS[k] ?? "var(--tx2)" }}>{byStatus[k]}</b> {k}
              </span>
            ))}
          </div>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
          <span className="mini-label">signals · 7d</span>
          <div className="pill-row">
            {Object.entries(funnel.signals_7d).map(([k, n]) => (
              <span key={k} className="pill">
                <b style={{ color: n > 0 ? "var(--amber)" : "var(--tx3)" }}>{n}</b>{" "}
                {SIGNAL_LABELS[k] ?? k}
              </span>
            ))}
          </div>
        </div>
        <div style={{ borderTop: "1px solid var(--bd)", paddingTop: 11 }}>
          <span className="mini-label">open memos</span>
          {funnel.memos.open.length === 0
            ? <div className="acc-none">none open</div> : (
            <table className="tbl" style={{ marginTop: 8, fontFamily: "var(--sans)" }}>
              <tbody>
                {funnel.memos.open.map((m) => {
                  const tier = tierBadge(m.conviction_tier);
                  return (
                    <tr key={m.memo_id}>
                      <td className="sym mono">{m.ticker}</td>
                      <td>{m.thesis_type}</td>
                      <td className="num mono" style={{ color: tier.color, fontWeight: 600, fontSize: 10 }}>
                        {tier.label}
                      </td>
                      <td className="num" style={{ color: "var(--tx3)", fontSize: 11 }}>
                        {relAge(m.created_at)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </div>
    );
  };
  return (
    <div className="panel">
      <div className="panel-head">
        <span>Research funnel</span>
        <span className="r">screen → memo → trade</span>
      </div>
      {body()}
    </div>
  );
}
