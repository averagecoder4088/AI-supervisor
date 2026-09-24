import Link from "next/link";
import { ApiError } from "@/api/client";
import { getActions, getFinalOutput, getMemory, getTimeline, getToolExecutions, settle } from "@/api/observation";
import { getRun, isActiveRun } from "@/api/runs";
import { getSupervisor } from "@/api/supervisors";
import type { Run, Supervisor } from "@/api/types";
import { ErrorPanel } from "@/components/ErrorPanel";
import { LiveRefresh } from "@/components/LiveRefresh";
import { RunNotFound } from "@/components/RunNotFound";
import { OrderStatus, StatusBadge } from "@/components/StatusBadge";
import { formatTimestamp } from "@/format";
import { POLL_INTERVAL_MS } from "@/polling";
import { EventInjector } from "./EventInjector";
import { HumanControls } from "./HumanControls";
import { InstructionForm } from "./InstructionForm";
import { loadWorkflowView } from "./controlMode";
import {
  ActionsSection,
  FinalOutputSection,
  MemorySection,
  TimelineSection,
  ToolExecutionsSection,
  WorkflowStatusSection,
} from "./ObservationSections";

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
    // Not proof of anything (the backend may just be down for a moment): keep retrying at the polling interval.
    return (
      <>
        <BackLink />
        <div className="mb-4 flex items-center justify-end gap-3">
          <LiveRefresh runActive workflowClosed={false} runStatus="unknown" retrying />
        </div>
        <ErrorPanel title="Could not load this run" error={error} />
      </>
    );
  }

  // Everything else is read in parallel and each source fails on its own: a supervisor lookup or an
  // observation source that fails must not hide the run or the other sections. The workflow's live status
  // is read ONCE (it also decides which human controls are offered; pause is not visible in run.status) and
  // only for an active run: the workflow of a finished run is closed.
  const [supervisorResult, workflowView, timeline, memory, actions, toolExecutions, finalOutput] = await Promise.all([
    settle(getSupervisor(run.supervisor_id)),
    isActiveRun(run) ? loadWorkflowView(run.id) : Promise.resolve(null),
    settle(getTimeline(run.id)),
    settle(getMemory(run.id)),
    settle(getActions(run.id)),
    settle(getToolExecutions(run.id)),
    settle(getFinalOutput(run.id)),
  ]);
  const supervisor: Supervisor | null = supervisorResult.ok ? supervisorResult.data : null;
  const loadedAt = new Date().toISOString();

  const fields: [string, React.ReactNode][] = [
    ["Order ID", <span key="o" className="font-medium">{run.order_id}</span>],
    ["Run ID", <span key="r" className="break-all font-mono text-xs">{run.id}</span>],
    [
      "Run status",
      <span key="s">
        <StatusBadge status={run.status} /> <span className="text-xs text-slate-500">(the application&apos;s record of the run)</span>
      </span>,
    ],
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
    [
      "Additional instructions",
      <span key="i">
        {run.run_instructions.length}{" "}
        <a href="#instructions" className="text-blue-700 hover:underline">
          (listed below)
        </a>
      </span>,
    ],
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
      <div className="mb-2 flex flex-wrap items-center gap-3">
        <h1 className="text-2xl font-semibold">Order {run.order_id}</h1>
        <StatusBadge status={run.status} />
        <div className="ml-auto flex items-center gap-3 text-xs text-slate-500">
          <span>Loaded {formatTimestamp(loadedAt)}</span>
          <LiveRefresh key={run.id} runActive={isActiveRun(run)} workflowClosed={workflowView?.mode === "closed"} runStatus={run.status} />
        </div>
      </div>
      <nav aria-label="Sections" className="mb-6 flex flex-wrap gap-x-4 gap-y-1 text-sm">
        {[
          ["#overview", "Overview"],
          ["#status", "Workflow status"],
          ["#controls", "Human controls"],
          ["#memory", "Memory"],
          ["#timeline", "Timeline"],
          ["#actions", "Actions"],
          ["#tools", "Tool executions"],
          ["#final-output", "Final output"],
          ["#instructions", "Instructions"],
          ["#events", "Inject an event"],
        ].map(([href, label]) => (
          <a key={href} href={href} className="text-blue-700 hover:underline">
            {label}
          </a>
        ))}
      </nav>

      <section id="overview" className="scroll-mt-4 rounded-lg border border-slate-200 bg-white p-5">
        <h2 className="mb-3 text-lg font-medium">Run overview</h2>
        <dl className="grid grid-cols-[max-content_1fr] gap-x-8 gap-y-2 text-sm">
          {fields.map(([label, value]) => (
            <div key={label} className="contents">
              <dt className="text-slate-500">{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
      </section>

      <WorkflowStatusSection run={run} view={workflowView} />

      <section id="controls" className="mt-6 scroll-mt-4 rounded-lg border border-slate-200 bg-white p-5">
        <h2 className="text-lg font-medium">Human controls</h2>
        <p className="mt-1 mb-4 text-sm text-slate-600">
          Pause, resume, interrupt or terminate this run&apos;s supervisor workflow. Each request goes through the backend
          to Temporal; the backend decides whether it is accepted.
        </p>
        <HumanControls runId={run.id} mode={workflowView?.mode ?? "inactive"} runStatus={run.status} />
      </section>

      <MemorySection result={memory} />
      <TimelineSection result={timeline} />
      <ActionsSection result={actions} />
      <ToolExecutionsSection result={toolExecutions} />
      <FinalOutputSection run={run} result={finalOutput} />

      <section id="instructions" className="mt-6 scroll-mt-4 rounded-lg border border-slate-200 bg-white p-5">
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

      <div id="events" className="mt-6 scroll-mt-4">
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
        This page shows what each source reported when it was loaded ({formatTimestamp(loadedAt)}). While the run is active
        it refreshes itself every {POLL_INTERVAL_MS / 1000} s and it stops when the run ends; Refresh reads everything
        again at once.
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
