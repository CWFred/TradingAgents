import type { Activity, Health, Section } from "../data/types";
import { isErr } from "../data/types";
import { nowLine } from "../lib/activity";
import { relAge } from "../lib/format";

export default function NowStrip({ activity, health }: {
  activity: Section<Activity> | null;
  health: Section<Health> | null;
}) {
  const a = activity && !isErr(activity) ? activity : null;
  const verdict = health && !isErr(health) ? health.verdict : "UNKNOWN";
  const line = nowLine(a, verdict);
  const started = a?.current?.started_at;
  return (
    <div className={`now-strip now-${line.state}`}>
      <span className="now-dot" aria-hidden="true" />
      <span className="now-text">{line.text}</span>
      {line.state === "busy" && started && (
        <span className="now-age">started {relAge(started)}</span>
      )}
      {activity && isErr(activity) && (
        <span className="now-age">{activity.error}</span>
      )}
    </div>
  );
}
