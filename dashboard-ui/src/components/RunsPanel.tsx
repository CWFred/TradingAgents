import type { Activity, Section } from "../data/types";
import { isErr } from "../data/types";
import { fmtDur, runOutcome } from "../lib/activity";
import { hhmmss } from "../lib/format";
import Unavail from "./Unavail";

export default function RunsPanel({ activity }: {
  activity: Section<Activity> | null;
}) {
  const a = activity && !isErr(activity) ? activity : null;
  return (
    <div className="panel">
      <div className="panel-head"><span>Runs</span></div>
      {activity && isErr(activity) ? <Unavail msg={activity.error} /> : (
        <>
          <div className="runs">
            {(a?.recent_runs ?? []).length === 0 && (
              <div className="panel-empty">no runs recorded yet</div>
            )}
            {(a?.recent_runs ?? []).map((r, i) => (
              <div key={`${r.job}-${r.started_at}-${i}`}
                className={`run-row${r.ok === false ? " run-bad" : ""}`}>
                <span className="t">{hhmmss(r.started_at)}</span>
                <span className="run-job">{r.job.replace(/_/g, " ")}</span>
                <span className="run-detail">
                  {r.reason && <span className="sub">{r.reason} · </span>}
                  {runOutcome(r)}
                </span>
                <span className="run-dur">{fmtDur(r.duration_s)}</span>
              </div>
            ))}
          </div>
          {(a?.next_work ?? []).length > 0 && (
            <div className="runs-next">
              {/* next_work timestamps are in the future — relAge() only
                  formats elapsed (past) time and reads as nonsense here
                  ("just now" for something hours away), so we show the
                  scheduled clock time and purpose without a relative age. */}
              {(a?.next_work ?? []).map((w) => (
                <div key={`${w.job}-${w.at}`} className="kv">
                  <span className="k">next {w.job.replace(/_/g, " ")}</span>
                  <span className="v">{hhmmss(w.at)}{" "}
                    <span className="sub">· {w.purpose}</span>
                  </span>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
