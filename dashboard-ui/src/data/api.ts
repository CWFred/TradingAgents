import type { EventItem, PnlResponse, Snapshot } from "./types";

async function getJson<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return r.json() as Promise<T>;
}

export const fetchSnapshot = () => getJson<Snapshot>("/api/snapshot");
export const fetchEvents = () => getJson<EventItem[]>("/api/events?limit=100");
export const fetchLog = (file: "out" | "err") =>
  getJson<{ file: string; text: string }>(`/api/logs?file=${file}&lines=200`);
export const fetchPnl = (sleeve: string) =>
  getJson<PnlResponse>(`/api/pnl?sleeve=${encodeURIComponent(sleeve)}`);
