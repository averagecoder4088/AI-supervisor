import Link from "next/link";
import { ApiError } from "@/api/client";
import { listRuns } from "@/api/runs";
import { getSupervisor, loadSupervisors } from "@/api/supervisors";
import type { Supervisor } from "@/api/types";
import { StartRunForm, type SupervisorOption } from "./StartRunForm";

export const dynamic = "force-dynamic";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export default async function StartRunPage({
  searchParams,
}: {
  searchParams: Promise<{ supervisorId?: string; created?: string }>;
}) {
  const { supervisorId = "", created } = await searchParams;

  // Known supervisors: those referenced by existing runs (no list endpoint exists) plus the one in the URL.
  const known = new Map<string, Supervisor>();
  try {
    for (const supervisor of (await loadSupervisors(await listRuns())).values()) known.set(supervisor.id, supervisor);
  } catch {
    // The form still works with a pasted ID if the run list cannot be loaded.
  }
  let selected: Supervisor | null = null;
  let selectedProblem = "";
  if (supervisorId) {
    if (!UUID.test(supervisorId)) {
      selectedProblem = "The supervisor ID in the link is not a valid UUID.";
    } else {
      try {
        selected = await getSupervisor(supervisorId);
        known.set(selected.id, selected);
      } catch (error) {
        selectedProblem =
          error instanceof ApiError && error.status === 404
            ? "The supervisor in the link does not exist."
            : `Could not load the supervisor from the link: ${error instanceof Error ? error.message : "unknown error"}`;
      }
    }
  }

  const options: SupervisorOption[] = [...known.values()]
    .sort((a, b) => a.name.localeCompare(b.name) || a.version - b.version)
    .map((s) => ({ id: s.id, label: `${s.name} (v${s.version})` }));
  const createdVersion = Number(created);

  return (
    <>
      <h1 className="mb-1 text-2xl font-semibold">Start run</h1>
      <p className="mb-6 text-sm text-slate-500">
        Starts one long-running supervisor workflow for one order.{" "}
        {options.length === 0 && (
          <>
            There is no supervisor yet: <Link href="/supervisors/new" className="text-blue-700 hover:underline">create one first</Link>.
          </>
        )}
      </p>

      {selected && created && (
        <div role="status" className="mb-6 rounded-lg border border-green-200 bg-green-50 p-4 text-sm text-green-900">
          <p className="font-medium">
            Supervisor “{selected.name}” created (version {selected.version}).
          </p>
          {createdVersion > 1 && (
            <p className="mt-1">
              A supervisor with this name already existed, so this is the next version. Earlier versions and their runs are unchanged.
            </p>
          )}
          <p className="mt-1">Now start a run with it.</p>
        </div>
      )}
      {selected && (
        <div className="mb-6 rounded-lg border border-slate-200 bg-white p-4 text-sm">
          <p>
            <span className="font-medium">{selected.name}</span> <span className="text-slate-500">version {selected.version}</span>
          </p>
          <p className="mt-1 text-slate-600">
            <span className="font-medium text-slate-700">Base instruction:</span> {selected.instructions}
          </p>
          <p className="mt-1 text-xs text-slate-500">Tools: {selected.enabled_tools.join(", ") || "none"}</p>
        </div>
      )}
      {selectedProblem && (
        <div role="alert" className="mb-6 rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
          {selectedProblem} Choose a supervisor below or paste an ID.
        </div>
      )}

      <StartRunForm options={options} selectedId={selected?.id ?? ""} />
    </>
  );
}
