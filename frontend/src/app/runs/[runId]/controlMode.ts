import { ApiError } from "@/api/client";
import { getRunStatus } from "@/api/runs";

/**
 * Which human controls an ACTIVE run should offer, from the workflow's own state (GET /status). The run's
 * `status` cannot say: a paused run still reads "running". Never throws, so a failing query cannot hide the page.
 *
 * - running: reasoning or sleeping           -> Pause, Interrupt, Terminate
 * - paused:  a human pause is in effect      -> Resume, Interrupt, Terminate
 * - finishing: the order reached a terminal status and the workflow is completing -> no controls
 * - closed:  the workflow is no longer running (409)                              -> no controls
 * - unknown: the query failed (Temporal or the worker is not answering)           -> all four; pause and
 *            resume are idempotent in the workflow, so offering both is harmless
 */
export type ControlMode = "running" | "paused" | "finishing" | "closed" | "unknown";

export async function loadControlMode(runId: string): Promise<ControlMode> {
  try {
    const { state } = await getRunStatus(runId);
    if (state === "paused") return "paused";
    if (state === "terminal") return "finishing";
    return "running";
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) return "closed";
    return "unknown";
  }
}
