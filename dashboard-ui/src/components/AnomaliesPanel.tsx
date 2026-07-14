import type { AnomalyEntry, Section } from "../data/types";
import { isErr } from "../data/types";
import { relAge } from "../lib/format";
import Unavail from "./Unavail";

export default function AnomaliesPanel({ anomalies }: {
  anomalies: Section<Record<string, AnomalyEntry>> | null;
}) {
  const body = () => {
    if (!anomalies) return <div className="panel-empty">waiting for snapshot…</div>;
    if (isErr(anomalies)) return <Unavail msg={anomalies.error} />;
    const rows = Object.entries(anomalies)
      .filter(([, v]) => v.count > 0)
      .sort(([, a], [, b]) => b.count - a.count);
    if (rows.length === 0) return <div className="panel-empty">none in last 7 days</div>;
    return (
      <table className="tbl padded">
        <tbody>
          {rows.map(([kind, v]) => (
            <tr key={kind}>
              <td>{kind}</td>
              <td className="num" style={{ color: v.count > 2 ? "var(--amber)" : "var(--tx2)", fontWeight: 600 }}>
                {v.count}
              </td>
              <td className="num" style={{ color: "var(--tx3)", fontSize: 11 }}>
                {relAge(v.last_at)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    );
  };
  return (
    <div className="panel">
      <div className="panel-head"><span>Anomalies</span><span className="r">7d</span></div>
      {body()}
    </div>
  );
}
