"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, useTransition } from "react";
import { CLOSED_CONFIRMATIONS, POLL_INTERVAL_MS } from "@/polling";

/**
 * The manual Refresh button plus the automatic polling of the run page, in ONE component so both share one
 * "a refresh is in flight" flag: an automatic tick never starts while a refresh (manual or automatic) is still
 * running, and there is exactly one timer per mounted page.
 *
 * Polling = `router.refresh()` every POLL_INTERVAL_MS. That re-runs the existing Server Components, so every
 * observation source is read by the server exactly as on a manual refresh; the browser never calls the
 * observation endpoints itself and keeps no copy of their data.
 *
 * - Polls while the run is active (`runActive`, from runs.status). A PAUSED workflow is still active
 *   (runs.status stays "running"), so it keeps being polled: events, instructions and controls can still change it.
 * - Stops when the run is no longer active (completed / terminated / failed), and when the workflow query has
 *   reported the workflow closed on CLOSED_CONFIRMATIONS consecutive renders.
 * - A failing status query or an unreachable backend is NOT a stop signal (`retrying` keeps polling); the same
 *   modest interval is the only retry, and a slow render simply delays the next tick.
 * - Skips ticks while the tab is hidden and refreshes once when it becomes visible again.
 */
export function LiveRefresh({
  runActive,
  workflowClosed,
  runStatus,
  retrying = false,
}: {
  runActive: boolean;
  workflowClosed: boolean;
  runStatus: string;
  /** The page could not even load the run (backend unreachable): keep trying. */
  retrying?: boolean;
}) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [closedStop, setClosedStop] = useState(false);
  const closedRenders = useRef(0);
  // The timer callback must see the latest props/flag without the effect (and so the timer) being recreated.
  const latest = useRef({ workflowClosed, pending });
  useEffect(() => {
    latest.current = { workflowClosed, pending };
  });

  const polling = runActive && !closedStop;
  useEffect(() => {
    if (!polling) return;
    const refresh = () => startTransition(() => router.refresh());
    const tick = () => {
      if (document.hidden || latest.current.pending) return;
      if (latest.current.workflowClosed) {
        closedRenders.current += 1;
        if (closedRenders.current >= CLOSED_CONFIRMATIONS) {
          setClosedStop(true);
          return;
        }
      } else {
        closedRenders.current = 0;
      }
      refresh();
    };
    const onVisible = () => {
      if (!document.hidden && !latest.current.pending) refresh();
    };
    const timer = setInterval(tick, POLL_INTERVAL_MS);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [polling, router]);

  const seconds = POLL_INTERVAL_MS / 1000;
  const label = !runActive
    ? `Live updates stopped — run ${runStatus}`
    : closedStop
      ? "Live updates stopped — workflow closed"
      : retrying
        ? `Live updates: backend not reachable, retrying every ${seconds}s`
        : workflowClosed
          ? "Live updates: workflow reported closed, confirming…"
          : `Live updates every ${seconds}s`;

  return (
    <>
      <span id="live-updates" className="text-xs text-slate-500">
        {label}
      </span>
      <button
        type="button"
        disabled={pending}
        onClick={() => startTransition(() => router.refresh())}
        className="rounded border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-800 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {pending ? "Refreshing…" : "Refresh"}
      </button>
    </>
  );
}
