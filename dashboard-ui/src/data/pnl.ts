import { useCallback, useEffect, useRef, useState } from "react";
import { fetchPnl } from "./api";
import type { PnlRow } from "./types";

export function usePnl(sleeve: string, active: boolean, intervalMs = 5000) {
  const [rows, setRows] = useState<Record<string, PnlRow>>({});
  const [loading, setLoading] = useState(false);
  const inFlight = useRef(false);

  useEffect(() => {
    if (!active) return;
    let alive = true;
    const tick = async () => {
      if (inFlight.current) return;
      inFlight.current = true;
      setLoading(true);
      try {
        const res = await fetchPnl(sleeve);
        if (!alive) return;
        const map: Record<string, PnlRow> = {};
        for (const r of res.positions) map[r.symbol] = r;
        setRows(map);
      } catch {
        /* leave last-good rows; the column shows — until next success */
      } finally {
        if (alive) setLoading(false);
        inFlight.current = false;
      }
    };
    void tick();
    const id = setInterval(() => void tick(), intervalMs);
    return () => { alive = false; clearInterval(id); };
  }, [sleeve, active, intervalMs]);

  return { rows, loading };
}

export type PnlMode = "dollar" | "pct";

export function usePnlMode(): [PnlMode, () => void] {
  const [mode, setMode] = useState<PnlMode>(
    () => (localStorage.getItem("pnlMode") as PnlMode) || "pct");
  const toggle = useCallback(() => {
    setMode((m) => {
      const next = m === "pct" ? "dollar" : "pct";
      localStorage.setItem("pnlMode", next);
      return next;
    });
  }, []);
  return [mode, toggle];
}
