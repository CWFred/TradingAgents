import { useState } from "react";
import { fetchLog } from "../data/api";

function LogSection({ file }: { file: "out" | "err" }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState<string | null>(null);

  const load = async () => {
    try {
      const r = await fetchLog(file);
      setText(r.text || "(empty)");
    } catch (e) {
      setText(`failed to load: ${e instanceof Error ? e.message : e}`);
    }
  };

  const toggle = () => {
    const opening = !open;
    setOpen(opening);
    if (opening && text === null) void load();
  };

  return (
    <div className="acc-row">
      <button type="button" className="acc-head" onClick={toggle}>
        <span className="l">
          <span className={`caret ${open ? "open" : ""}`}>▸</span>
          <span className="mono" style={{ fontSize: 12, color: "var(--tx2)" }}>
            ops.{file}.log
          </span>
        </span>
        {open && (
          <span className="btn-ghost" role="button"
            onClick={(e) => { e.stopPropagation(); void load(); }}>
            refresh
          </span>
        )}
      </button>
      {open && <pre className={`log-pre ${file === "err" ? "err" : ""}`}>{text ?? "loading…"}</pre>}
    </div>
  );
}

export default function LogsPanel() {
  return (
    <div className="panel">
      <div className="panel-head"><span>Logs</span></div>
      <LogSection file="out" />
      <LogSection file="err" />
    </div>
  );
}
