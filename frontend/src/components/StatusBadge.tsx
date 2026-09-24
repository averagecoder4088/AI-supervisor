import { Badge, type Tone } from "./Badge";

const STYLES: Record<string, string> = {
  starting: "bg-amber-100 text-amber-800",
  running: "bg-blue-100 text-blue-800",
  active: "bg-blue-100 text-blue-800",
  completed: "bg-green-100 text-green-800",
  terminated: "bg-slate-200 text-slate-700",
  failed: "bg-red-100 text-red-800",
};

/** The run's lifecycle status (starting / running / completed / terminated / failed). */
export function StatusBadge({ status }: { status: string }) {
  const style = STYLES[status] ?? "bg-slate-100 text-slate-700";
  return (
    <span className={`inline-block rounded-full px-2.5 py-0.5 text-xs font-medium ${style}`}>{status}</span>
  );
}

/** The order's business status (created / shipped / delivered / ...), or a dash while unknown. */
export function OrderStatus({ status }: { status: string | null }) {
  if (!status) return <span className="text-slate-400">—</span>;
  return <span className="rounded bg-slate-100 px-2 py-0.5 font-mono text-xs text-slate-700">{status}</span>;
}

const WORKFLOW_TONE: Record<string, Tone> = { reasoning: "blue", sleeping: "slate", paused: "amber", terminal: "green" };

/** The workflow's OWN live state (reasoning / sleeping / paused / terminal), not the run's status. */
export function WorkflowStateBadge({ state }: { state: string }) {
  return <Badge tone={WORKFLOW_TONE[state] ?? "slate"}>{state}</Badge>;
}
