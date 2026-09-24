import { listRuns } from "@/api/runs";
import { loadSupervisors } from "@/api/supervisors";
import type { Run } from "@/api/types";
import { ErrorPanel } from "@/components/ErrorPanel";
import { formatTimestamp } from "@/format";

export const dynamic = "force-dynamic";

// The backend has no "list supervisors" endpoint (only POST and GET by id), so this page shows
// the supervisors that existing runs reference. Creating supervisors comes in a later step.
export default async function SupervisorsPage() {
  let runs: Run[];
  try {
    runs = await listRuns();
  } catch (error) {
    return (
      <>
        <h1 className="mb-6 text-2xl font-semibold">Supervisors</h1>
        <ErrorPanel title="Could not load supervisors" error={error} />
      </>
    );
  }

  const supervisors = [...(await loadSupervisors(runs)).values()].sort(
    (a, b) => a.name.localeCompare(b.name) || a.version - b.version,
  );
  const runCount = (id: string) => runs.filter((run) => run.supervisor_id === id).length;

  return (
    <>
      <h1 className="mb-1 text-2xl font-semibold">Supervisors</h1>
      <p className="mb-6 text-sm text-slate-500">
        Supervisors used by existing runs. The backend has no supervisor list endpoint yet, so a supervisor with no
        runs is not shown here. Creating supervisors comes in a later step.
      </p>

      {supervisors.length === 0 ? (
        <div className="rounded-lg border border-dashed border-slate-300 bg-white p-10 text-center">
          <p className="font-medium">No supervisors in use</p>
          <p className="mt-1 text-sm text-slate-500">Supervisors appear here once a run references them.</p>
        </div>
      ) : (
        <ul className="space-y-4">
          {supervisors.map((s) => (
            <li key={s.id} className="rounded-lg border border-slate-200 bg-white p-5">
              <div className="flex flex-wrap items-baseline gap-x-3">
                <h2 className="text-lg font-medium">{s.name}</h2>
                <span className="text-sm text-slate-500">version {s.version}</span>
                <span className="text-sm text-slate-500">
                  {runCount(s.id)} run{runCount(s.id) === 1 ? "" : "s"}
                </span>
              </div>
              {s.description && <p className="mt-1 text-sm text-slate-600">{s.description}</p>}
              <dl className="mt-3 grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 text-sm">
                <dt className="text-slate-500">ID</dt>
                <dd className="break-all font-mono text-xs">{s.id}</dd>
                <dt className="text-slate-500">Enabled tools</dt>
                <dd>{s.enabled_tools.join(", ") || "—"}</dd>
                <dt className="text-slate-500">Wake interval (min / default / max)</dt>
                <dd>
                  {s.min_wake_interval_minutes} / {s.default_wake_interval_minutes} / {s.max_wake_interval_minutes}{" "}
                  minutes
                </dd>
                <dt className="text-slate-500">Terminal order statuses</dt>
                <dd>{s.terminal_order_statuses.join(", ") || "—"}</dd>
                <dt className="text-slate-500">Created</dt>
                <dd>{formatTimestamp(s.created_at)}</dd>
              </dl>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
