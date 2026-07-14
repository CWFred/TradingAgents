import { useEffect, useReducer, useRef } from "react";
import { fetchEvents, fetchSnapshot } from "./api";
import type { EventItem, Snapshot } from "./types";

export const DISCONNECT_AFTER = 3;

export interface PollState {
  snapshot: Snapshot | null;
  events: EventItem[];
  lastGoodAt: number | null;
  failures: number;
}

export const initialPollState: PollState = {
  snapshot: null, events: [], lastGoodAt: null, failures: 0,
};

export type PollAction =
  | { type: "success"; snapshot: Snapshot; events: EventItem[]; at: number }
  | { type: "failure" };

export function pollReducer(s: PollState, a: PollAction): PollState {
  if (a.type === "success") {
    return { snapshot: a.snapshot, events: a.events, lastGoodAt: a.at, failures: 0 };
  }
  return { ...s, failures: s.failures + 1 };
}

export const isDisconnected = (s: PollState) => s.failures >= DISCONNECT_AFTER;

export function usePoll(intervalMs = 5000): PollState {
  const [state, dispatch] = useReducer(pollReducer, initialPollState);
  const inFlight = useRef(false); // skip a tick while a fetch is outstanding

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      if (inFlight.current) return;
      inFlight.current = true;
      try {
        const [snapshot, events] = await Promise.all([fetchSnapshot(), fetchEvents()]);
        if (alive) dispatch({ type: "success", snapshot, events, at: Date.now() });
      } catch {
        if (alive) dispatch({ type: "failure" });
      } finally {
        inFlight.current = false;
      }
    };
    void tick();
    const id = setInterval(() => void tick(), intervalMs);
    return () => { alive = false; clearInterval(id); };
  }, [intervalMs]);

  return state;
}
