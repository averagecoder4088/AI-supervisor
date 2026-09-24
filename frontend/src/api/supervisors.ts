import { apiGet } from "./client";
import type { Run, Supervisor } from "./types";

/** GET /api/supervisors/{id}. The backend has no supervisor list endpoint. */
export function getSupervisor(supervisorId: string): Promise<Supervisor> {
  return apiGet<Supervisor>(`/api/supervisors/${encodeURIComponent(supervisorId)}`);
}

/**
 * Look up each distinct supervisor referenced by the given runs. A supervisor that cannot be
 * loaded is simply absent from the map (callers fall back to the id), so one failed lookup never
 * hides the runs themselves.
 */
export async function loadSupervisors(runs: Run[]): Promise<Map<string, Supervisor>> {
  const ids = [...new Set(runs.map((run) => run.supervisor_id))];
  const results = await Promise.allSettled(ids.map((id) => getSupervisor(id)));
  const found = new Map<string, Supervisor>();
  results.forEach((result, index) => {
    if (result.status === "fulfilled") found.set(ids[index], result.value);
  });
  return found;
}
