import { useMemo, useState } from "react";
import type { EventItem } from "../data/types";
import { kindClass } from "../lib/colors";
import { hhmmss, relAge } from "../lib/format";

export default function ActivityFeed({ events }: { events: EventItem[] }) {
  const [filter, setFilter] = useState("all");
  const kinds = useMemo(
    () => [...new Set(events.map((e) => e.kind))].sort(), [events]);
  const shown = filter === "all" ? events : events.filter((e) => e.kind === filter);
  return (
    <div className="panel">
      <div className="panel-head" style={{ padding: "11px 14px" }}>
        <span>Activity</span>
        <select className="filter" value={filter} onChange={(e) => setFilter(e.target.value)}>
          <option value="all">all kinds</option>
          {kinds.map((k) => <option key={k} value={k}>{k}</option>)}
        </select>
      </div>
      <div className="feed">
        {shown.length === 0 && <div className="panel-empty">no events</div>}
        {shown.map((e) => (
          <div key={`${e.source}-${e.id}`} className="feed-row">
            <span className="t">{hhmmss(e.at)}</span>
            <span className={`kind ${kindClass(e.kind)}`}>{e.kind}</span>
            <span className="txt">{e.text}</span>
            <span className="age">{relAge(e.at)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
