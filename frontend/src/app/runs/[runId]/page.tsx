import Link from "next/link";
import { ApiError } from "@/api/client";
import { getRun, isActiveRun } from "@/api/runs";
import { getSupervisor } from "@/api/supervisors";
import type { Run, Supervisor } from "@/api/types";
import { ErrorPanel } from "@/components/ErrorPanel";
import { RunNotFound } from "@/components/RunNotFound";
import { OrderStatus, StatusBadge } from "@/components/StatusBadge";
import { formatTimestamp } from "@/format";
import { EventInjector } from "./EventInjector";
import { InstructionForm } from "./InstructionForm";

export const dynamic = "force-dynamic";

export default async function RunDetailPage({
  params,
  searchParams,
}: {
  params: Promise<{ runId: string }>;
  searchParams: Promise<{ started?: string }>;
}) {
  const { runId } = await params;
  const { started } = await searchParams;

  let run: Run;
  try {
    run = await getRun(runId);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return <RunNotFound />;
    return (
      <>
        <BackLink />
        <ErrorPanel title="Could not load this run" error={error} />
      </>
    );
  }

  // A supervisor lookup failure must not hide the run: fall back to the id.
  let supervisor: Supervisor | null = null;
  try {
    supervisor = await getSupervisor(run.supervisor_id);
  } catch {
    supervisor = null;
  }

  const fields: [string, React.ReactNode][] = [
    ["Order ID", <span key="o" className="font-medium">{run.order_id}</span>],
    ["Run ID", <span key="r" className="break-all font-mono text-xs">{run.id}</span>],
    ["Status", <StatusBadge key="s" status={run.status} />],
    ["Order status", <OrderStatus key="os" status={run.order_status} />],
    [
      "Supervisor",
      <span key="sv">
        {supervisor ? supervisor.name : <span className="font-mono text-xs">{run.supervisor_id}</span>}{" "}
        <span className="text-slate-500">version {run.supervisor_version}</span>
      </span>,
    ],
    ["Created", formatTimestamp(run.created_at)],
    ["Started", formatTimestamp(run.started_at)],
    ["Completed", formatTimestamp(run.completed_at)],
  ];

  return (
    <>
      <BackLink />
      {started && (
        <div role="status" className="mb-6 rounded-lg border border-green-200 bg-green-50 p-4 text-sm text-green-900">
          <p className="font-medium">Run started.</p>
          <p className="mt-1">
            The supervisor workflow <span className="font-mono">order-{run.order_id}</span> is now running on Temporal.
          </p>
        </div>
      )}
      <div className="mb-6 flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-semibold">Order {run.order_id}</h1>
        <StatusBadge status={run.status} />
      </div>

      <section className="rounded-lg border border-slate-200 bg-white p-5">
        <h2 className="mb-3 text-lg font-medium">Run</h2>
        <dl className="grid grid-cols-[max-content_1fr] gap-x-8 gap-y-2 text-sm">
          {fields.map(([label, value]) => (
            <div key={label} className="contents">
              <dt className="text-slate-500">{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
      </section>

      <section className="mt-6 rounded-lg border border-slate-200 bg-white p-5">
        <h2 className="text-lg font-medium">Additional instructions for this run ({run.run_instructions.length})</h2>
        <p className="mt-1 mb-3 text-sm text-slate-600">
          Instructions that apply to <span className="font-medium">this order only</span>. They are separate from the
          supervisor&apos;s base instruction, which applies to every run of that supervisor and stays unchanged. Adding an
          instruction sends it to this run and normally wakes the supervisor to re-evaluate the order. If the run is
          paused, the instruction is recorded but supervisor reasoning stays paused until the run is resumed.
        </p>
        {supervisor && (
          <p className="mb-3 rounded bg-slate-50 p-3 text-xs text-slate-600">
            <span className="font-medium text-slate-700">Supervisor base instruction (not editable here):</span>{" "}
            {supervisor.instructions}
          </p>
        )}
        {run.run_instructions.length === 0 ? (
          <p className="text-sm text-slate-500">No additional instructions yet.</p>
        ) : (
          <ol className="list-decimal space-y-2 pl-5 text-sm">
            {run.run_instructions.map((instruction, index) => (
              <li key={index}>
                {instruction.text ?? JSON.stringify(instruction)}
                {instruction.added_at && (
                  <span className="ml-2 text-xs text-slate-500">added {formatTimestamp(instruction.added_at)}</span>
                )}
              </li>
            ))}
          </ol>
        )}
        {isActiveRun(run) ? (
          <InstructionForm runId={run.id} />
        ) : (
          <p className="mt-4 border-t border-slate-100 pt-4 text-sm text-slate-600">
            This run is <span className="font-medium">{run.status}</span> and no longer accepts instructions. Only an
            active run can be given new ones.
          </p>
        )}
      </section>

      <div className="mt-6">
        {isActiveRun(run) ? (
          <EventInjector runId={run.id} workflowId={`order-${run.order_id}`} />
        ) : (
          <section className="rounded-lg border border-slate-200 bg-white p-5 text-sm text-slate-600">
            <h2 className="mb-1 text-lg font-medium text-slate-900">Inject an event</h2>
            This run is <span className="font-medium">{run.status}</span>. Events can only be injected into an active run.
          </section>
        )}
      </div>

      <p className="mt-6 text-sm text-slate-500">
        Live status, timeline, memory, actions and final output are added in the next step.
      </p>
    </>
  );
}

function BackLink() {
  return (
    <Link href="/" className="mb-4 inline-block text-sm text-blue-700 hover:underline">
      ← Dashboard
    </Link>
  );
}
