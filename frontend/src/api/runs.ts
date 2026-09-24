import { apiGet, apiPost } from "./client";
import type { Accepted, EventCreateBody, InstructionCreateBody, Run, RunCreateBody } from "./types";

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

/**
 * POST /api/runs/{run_id}/events: the backend sends the event into the run's Temporal workflow as a
 * Signal. 202 means accepted at the workflow boundary; the workflow records it a moment later.
 */
export function injectEvent(runId: string, body: EventCreateBody): Promise<Accepted> {
  return apiPost<Accepted>(`/api/runs/${encodeURIComponent(runId)}/events`, body);
}

/**
 * POST /api/runs/{run_id}/instructions: the backend sends the instruction into the run's workflow as a
 * Signal. The workflow stores it on the run, persists it a moment later, and normally wakes the supervisor
 * to re-evaluate (a paused run records it but keeps supervisor reasoning paused until resumed). 202 means
 * accepted at the workflow boundary, not yet recorded.
 */
export function addInstruction(runId: string, body: InstructionCreateBody): Promise<Accepted> {
  return apiPost<Accepted>(`/api/runs/${encodeURIComponent(runId)}/instructions`, body);
}
