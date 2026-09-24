import { ApiError } from "@/api/client";
import { getRunStatus } from "@/api/runs";
import type { WorkflowStatus } from "@/api/types";

/**
 * Which human controls an ACTIVE run should offer, from the workflow's own state (GET /status). The run's
 * `status` cannot say: a paused run still reads "running".
 *
 * - running: reasoning or sleeping           -> Pause, Interrupt, Terminate
 * - paused:  a human pause is in effect      -> Resume, Interrupt, Terminate
 * - finishing: the order reached a terminal status and the workflow is completing -> no controls
 * - closed:  the workflow is no longer running (409)                              -> no controls
 * - unavailable: the query failed (Temporal, the worker or the backend is not answering). The state is NOT
 *            guessed: the controls are shown disabled, because Pause vs Resume cannot be chosen safely.
 */
export type ControlMode = "running" | "paused" | "finishing" | "closed" | "unavailable";

/** The one /status read of a page render: what the Workflow Status section shows and what the controls offer. */
export interface WorkflowView {
  mode: ControlMode;
  /** The workflow's live state; null when it could not be read (closed or unavailable). */
  status: WorkflowStatus | null;
  /** Why it could not be read; null when it was. */
  error: unknown;
}

/** Never throws, so a failing query cannot hide the page. */
export async function loadWorkflowView(runId: string): Promise<WorkflowView> {
  try {
    const status = await getRunStatus(runId);
    const mode: ControlMode = status.state === "paused" ? "paused" : status.state === "terminal" ? "finishing" : "running";
    return { mode, status, error: null };
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) return { mode: "closed", status: null, error };
    return { mode: "unavailable", status: null, error };
  }
}
