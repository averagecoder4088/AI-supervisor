import Link from "next/link";
import { isActiveRun, listRuns } from "@/api/runs";
import { loadSupervisors } from "@/api/supervisors";
import type { Run } from "@/api/types";
import { ErrorPanel } from "@/components/ErrorPanel";
import { RunsTable } from "@/components/RunsTable";

// Always render against the live backend, never at build time.
export const dynamic = "force-dynamic";

export default async function DashboardPage() {
  let runs: Run[];
  try {
    runs = await listRuns();
  } catch (error) {
    return (
      <>
        <h1 className="mb-6 text-2xl font-semibold">Dashboard</h1>
        <ErrorPanel title="Could not load runs" error={error} />
      </>
    );
  }

  if (runs.length === 0) {
    return (
      <>
        <h1 className="mb-6 text-2xl font-semibold">Dashboard</h1>
        <div className="rounded-lg border border-dashed border-slate-300 bg-white p-10 text-center">
          <p className="font-medium">No runs yet</p>
          <p className="mt-1 text-sm text-slate-500">
            Create a supervisor, then start a run for an order.
          </p>
          <div className="mt-4 flex justify-center gap-3 text-sm">
            <Link href="/supervisors/new" className="rounded border border-slate-300 px-3 py-1.5 hover:bg-slate-50">
              Create supervisor
            </Link>
            <Link href="/runs/new" className="rounded bg-blue-700 px-3 py-1.5 text-white hover:bg-blue-800">
              Start run
            </Link>
          </div>
        </div>
      </>
    );
  }

  const supervisors = await loadSupervisors(runs);
  const newestFirst = [...runs].sort((a, b) => b.created_at.localeCompare(a.created_at));
  const active = newestFirst.filter(isActiveRun);
  const finished = newestFirst.filter((run) => !isActiveRun(run));
  const completed = finished.filter((run) => run.status === "completed").length;

  return (
    <>
      <div className="mb-6 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="mb-1 text-2xl font-semibold">Dashboard</h1>
          <p className="text-sm text-slate-500">
            {runs.length} run{runs.length === 1 ? "" : "s"}: {active.length} active, {completed} completed
            {finished.length - completed > 0 ? `, ${finished.length - completed} terminated or failed` : ""}.
          </p>
          <p className="mt-1 max-w-2xl text-xs text-slate-500">
            Run status is the application&apos;s own record. A paused supervisor still shows as running here: open a run to
            see its live workflow state.
          </p>
        </div>
        <div className="flex gap-3 text-sm">
          <Link href="/supervisors/new" className="rounded border border-slate-300 bg-white px-3 py-1.5 hover:bg-slate-50">
            Create supervisor
          </Link>
          <Link href="/runs/new" className="rounded bg-blue-700 px-3 py-1.5 text-white hover:bg-blue-800">
            Start run
          </Link>
        </div>
      </div>

      <section className="mb-8">
        <h2 className="mb-2 text-lg font-medium">Active runs ({active.length})</h2>
        {active.length > 0 ? (
          <RunsTable runs={active} supervisors={supervisors} />
        ) : (
          <p className="rounded-lg border border-slate-200 bg-white p-4 text-sm text-slate-500">No active runs.</p>
        )}
      </section>

      <section>
        <h2 className="mb-2 text-lg font-medium">Completed and ended runs ({finished.length})</h2>
        {finished.length > 0 ? (
          <RunsTable runs={finished} supervisors={supervisors} />
        ) : (
          <p className="rounded-lg border border-slate-200 bg-white p-4 text-sm text-slate-500">
            No completed runs yet.
          </p>
        )}
      </section>
    </>
  );
}
