import type { Funnel, Section } from "../data/types";
import { isErr } from "../data/types";
import { hhmmss, relAge } from "../lib/format";
import Unavail from "./Unavail";

// The overnight research window runs 00:00–08:00 local — same convention
// the overnight services themselves use.
export const inOvernightWindow = (d = new Date()) => d.getHours() < 8;

export default function OvernightPanel({ funnel }: { funnel: Section<Funnel> | null }) {
  const o = funnel && !isErr(funnel) ? funnel.overnight : null;
  return (
    <div className="panel">
      <div className="panel-head">
        <span>Overnight</span>
        {o?.paused ? <span className="tag-paused">◼ PAUSED</span>
          : inOvernightWindow() ? <span className="tag-active">WINDOW ACTIVE</span> : null}
      </div>
      {funnel && isErr(funnel) ? <Unavail msg={funnel.error} /> : (
        <div className="kv-rows">
          <div className="kv">
            <span className="k">last vetting</span>
            <span className="v">
              {o?.last_vetting_run ? hhmmss(o.last_vetting_run.at) : "—"}{" "}
              <span className="sub">· {relAge(o?.last_vetting_run?.at)}</span>
            </span>
          </div>
          <div className="kv">
            <span className="k">last drain</span>
            <span className="v">
              {o?.last_drain_run ? hhmmss(o.last_drain_run.at) : "—"}{" "}
              <span className="sub">· {relAge(o?.last_drain_run?.at)}</span>
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
