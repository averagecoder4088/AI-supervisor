// The read-only observation endpoints (backend/app/api/observation.py). History comes from PostgreSQL;
// the live workflow status comes from Temporal (see runs.ts getRunStatus). No polling: each page render
// reads once, and the Refresh button re-renders.

import { ApiError, apiGet } from "./client";
import type { FinalOutputResponse, MemorySnapshot, RunAction, TimelineEntry, ToolExecution } from "./types";

/** One observation source's outcome, so a failure of one source cannot break the whole page. */
export type Loaded<T> = { ok: true; data: T } | { ok: false; error: unknown };

export async function settle<T>(promise: Promise<T>): Promise<Loaded<T>> {
  try {
    return { ok: true, data: await promise };
  } catch (error) {
    return { ok: false, error };
  }
}

const path = (runId: string, what: string) => `/api/runs/${encodeURIComponent(runId)}/${what}`;

/** Pull a list out of `{run_id, <key>: [...]}`; a body of a different shape is reported, not rendered. */
async function list<T>(runId: string, what: string, key: string): Promise<T[]> {
  const url = path(runId, what);
  const body = await apiGet<Record<string, unknown>>(url);
  const items = body?.[key];
  if (!Array.isArray(items)) {
    throw new ApiError(`The backend answered, but the response had no "${key}" list.`, 200, "MALFORMED_RESPONSE", url);
  }
  return items as T[];
}

/** GET /timeline, oldest first. */
export const getTimeline = (runId: string) => list<TimelineEntry>(runId, "timeline", "entries");

/** GET /memory: every snapshot, oldest first (the last one is the current memory). */
export const getMemory = (runId: string) => list<MemorySnapshot>(runId, "memory", "snapshots");

/** GET /actions, oldest first. */
export const getActions = (runId: string) => list<RunAction>(runId, "actions", "actions");

/** GET /tool-executions, oldest first. */
export const getToolExecutions = (runId: string) => list<ToolExecution>(runId, "tool-executions", "tool_executions");

/** GET /final-output: `final_output` is null until the run completes. */
export async function getFinalOutput(runId: string): Promise<FinalOutputResponse> {
  const url = path(runId, "final-output");
  const body = await apiGet<FinalOutputResponse>(url);
  if (body === null || typeof body !== "object" || !("final_output" in body)) {
    throw new ApiError('The backend answered, but the response had no "final_output" field.', 200, "MALFORMED_RESPONSE", url);
  }
  return body;
}
