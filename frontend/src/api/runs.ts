import { apiGet, apiPost } from "./client";
import type { Run, RunCreateBody } from "./types";

// Mirrors the backend's ACTIVE_STATUSES (backend/app/api/runs.py); "active" is the legacy default.
const ACTIVE_STATUSES = new Set(["starting", "running", "active"]);

export function isActiveRun(run: Run): boolean {
  return ACTIVE_STATUSES.has(run.status);
}

/** GET /api/runs (a plain array, oldest first). */
export function listRuns(): Promise<Run[]> {
  return apiGet<Run[]>("/api/runs");
}

/** GET /api/runs/{run_id}. A missing run is an ApiError with status 404 / code RUN_NOT_FOUND. */
export function getRun(runId: string): Promise<Run> {
  return apiGet<Run>(`/api/runs/${encodeURIComponent(runId)}`);
}

/** POST /api/runs: records the run and starts its Temporal workflow (201 = running). */
export function createRun(body: RunCreateBody): Promise<Run> {
  return apiPost<Run>("/api/runs", body);
}
