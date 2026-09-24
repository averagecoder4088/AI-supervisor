// The read-only observation sections of the run page. Server components: each takes ONE source's outcome
// (Loaded<T>), so a source that failed shows "<name> unavailable" and the rest of the page still renders.
// Field names and vocabularies mirror the backend (backend/app/api/schemas.py, temporal/workflows.py).

import type { Loaded } from "@/api/observation";
import type {
  FinalOutputResponse,
  MemorySnapshot,
  Run,
  RunAction,
  TimelineEntry,
  ToolExecution,
  WorkflowStatus,
} from "@/api/types";
import { Badge, type Tone } from "@/components/Badge";
import { SectionUnavailable } from "@/components/SectionUnavailable";
import { WorkflowStateBadge } from "@/components/StatusBadge";
import { formatTimestamp, shortId } from "@/format";
import type { WorkflowView } from "./controlMode";

const CARD = "mt-6 rounded-lg border p-5";
const CARD_PLAIN = "border-slate-200 bg-white";
const CARD_FINAL = "border-green-300 bg-green-50"; // the end-of-run artifact

function Section({
  id,
  title,
  note,
  tone,
  children,
}: {
  id: string;
  title: string;
  note?: React.ReactNode;
  /** "final": the end-of-run artifact gets a green tint so it reads as the result. */
  tone?: "final";
  children: React.ReactNode;
}) {
  return (
    <section id={id} className={`${CARD} scroll-mt-4 ${tone === "final" ? CARD_FINAL : CARD_PLAIN}`}>
      <h2 className="text-lg font-medium">{title}</h2>
      {note && <p className="mt-1 mb-3 text-sm text-slate-600">{note}</p>}
      {!note && <div className="mb-3" />}
      {children}
    </section>
  );
}

const Empty = ({ children }: { children: React.ReactNode }) => <p className="text-sm text-slate-500">{children}</p>;

/** Text for any JSON value: strings as they are, everything else as compact JSON. */
function show(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function Json({ value }: { value: unknown }) {
  let text: string;
  try {
    text = JSON.stringify(value, null, 2) ?? "null";
  } catch {
    text = String(value);
  }
  return <pre className="max-h-64 overflow-auto rounded bg-slate-50 p-2 text-xs">{text}</pre>;
}

function Dl({ rows }: { rows: [string, React.ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-8 gap-y-2 text-sm">
      {rows.map(([label, value]) => (
        <div key={label} className="contents">
          <dt className="text-slate-500">{label}</dt>
          <dd className="min-w-0 break-words">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function StringList({ items, empty }: { items: unknown; empty: string }) {
  const list = Array.isArray(items) ? items : [];
  if (list.length === 0) return <span className="text-slate-500">{empty}</span>;
  return (
    <ul className="list-disc space-y-1 pl-5">
      {list.map((item, index) => (
        <li key={index}>{show(item)}</li>
      ))}
    </ul>
  );
}

// ------------------------------------------------------------------ 2. workflow status

export function WorkflowStatusSection({ run, view }: { run: Run; view: WorkflowView | null }) {
  const note = (
    <>
      Live state of the supervisor workflow, read from Temporal. It is not the same as the run status above: a{" "}
      <span className="font-medium">paused</span> workflow still has run status <span className="font-medium">running</span>.
    </>
  );
  let body: React.ReactNode;
  if (view === null) {
    body = (
      <Empty>
        Live workflow status is not available: the run is <span className="font-medium">{run.status}</span>, so its workflow
        is closed. The recorded history below is still available.
      </Empty>
    );
  } else if (view.status) {
    body = <WorkflowStatusGrid status={view.status} />;
  } else if (view.mode === "closed") {
    body = (
      <Empty>
        The workflow is not running (Temporal reports it closed), so there is no live state to show. The recorded history
        below is still available.
      </Empty>
    );
  } else {
    body = <SectionUnavailable what="Workflow status" error={view.error} />;
  }
  return (
    <Section id="status" title="Workflow status" note={note}>
      {body}
    </Section>
  );
}

function WorkflowStatusGrid({ status }: { status: WorkflowStatus }) {
  return (
    <Dl
      rows={[
        ["Workflow state", <WorkflowStateBadge key="s" state={status.state} />],
        ["Last cycle outcome", show(status.last_cycle_outcome)],
        ["Last wake reason", show(status.last_wake_reason)],
        ["Next scheduled wake", formatTimestamp(status.next_wake_at)],
        ["Reasoning cycles", String(status.reasoning_count)],
        ["Interrupts", String(status.interrupt_count)],
        ["Events received", `${status.events_received} (${Array.isArray(status.pending_events) ? status.pending_events.length : 0} not yet seen by a reasoning cycle)`],
        ["Terminal order status reached", status.terminal_order_status_reached ? "yes" : "no"],
      ]}
    />
  );
}

// ------------------------------------------------------------------ 3. timeline

const ENTRY_TONE: Record<string, Tone> = {
  event: "blue",
  action: "green",
  control: "amber",
  instruction: "purple",
  decision: "indigo",
  system: "slate",
};

export function TimelineSection({ result }: { result: Loaded<TimelineEntry[]> }) {
  if (!result.ok) {
    return (
      <Section id="timeline" title="Timeline">
        <SectionUnavailable what="Timeline" error={result.error} />
      </Section>
    );
  }
  const entries = result.data;
  return (
    <Section
      id="timeline"
      title={`Timeline (${entries.length})`}
      note="Everything that happened to this run, OLDEST FIRST: events, the supervisor's decisions and actions, human controls, instructions and system notes."
    >
      {entries.length === 0 ? (
        <Empty>Nothing recorded yet.</Empty>
      ) : (
        <ul className="divide-y divide-slate-100 text-sm">
          {entries.map((entry) => (
            <li key={entry.id} className="flex flex-wrap items-baseline gap-x-3 gap-y-1 py-2">
              <span className="w-44 shrink-0 font-mono text-xs text-slate-500">{formatTimestamp(entry.created_at)}</span>
              <span className="w-24 shrink-0">
                <Badge tone={ENTRY_TONE[entry.entry_type] ?? "slate"}>{entry.entry_type}</Badge>
              </span>
              <span className="min-w-0 flex-1 basis-64 break-words">{entry.message}</span>
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

// ------------------------------------------------------------------ 4. memory

export function MemorySection({ result }: { result: Loaded<MemorySnapshot[]> }) {
  const note = (
    <>
      The supervisor&apos;s <span className="font-medium">compact working memory</span>: a short rewritten summary, not the
      complete history (that is the Timeline).
    </>
  );
  if (!result.ok) {
    return (
      <Section id="memory" title="Memory" note={note}>
        <SectionUnavailable what="Memory" error={result.error} />
      </Section>
    );
  }
  const snapshots = result.data;
  if (snapshots.length === 0) {
    return (
      <Section id="memory" title="Memory" note={note}>
        <Empty>No memory snapshot has been recorded yet.</Empty>
      </Section>
    );
  }
  const latest = snapshots[snapshots.length - 1];
  const earlier = snapshots.slice(0, -1).reverse(); // newest of the earlier ones first
  return (
    <Section id="memory" title="Memory" note={note}>
      <p className="mb-2 text-xs font-medium tracking-wide text-slate-500 uppercase">
        Latest snapshot ({snapshots.length} of {snapshots.length}) · {formatTimestamp(latest.created_at)}
      </p>
      <div className="rounded border border-indigo-200 bg-indigo-50/50 p-4">
        <MemoryFields memory={latest.memory} />
      </div>
      {earlier.length > 0 && (
        <details className="mt-4 text-sm">
          <summary className="cursor-pointer text-blue-700">Earlier snapshots ({earlier.length})</summary>
          <div className="mt-3 space-y-3">
            {earlier.map((snapshot, index) => (
              <div key={snapshot.id} className="rounded border border-slate-200 p-3">
                <p className="mb-2 text-xs text-slate-500">
                  Snapshot {earlier.length - index} of {snapshots.length} · {formatTimestamp(snapshot.created_at)}
                </p>
                <MemoryFields memory={snapshot.memory} />
              </div>
            ))}
          </div>
        </details>
      )}
    </Section>
  );
}

function MemoryFields({ memory }: { memory: Record<string, unknown> }) {
  const m = memory && typeof memory === "object" ? memory : {};
  return (
    <Dl
      rows={[
        ["Situation summary", m.situation_summary ? show(m.situation_summary) : <span key="s" className="text-slate-500">— (empty)</span>],
        ["Open concerns", <StringList key="c" items={m.open_concerns} empty="none" />],
        ["Last action", show(m.last_action)],
        ["Last wake reason", show(m.last_wake_reason)],
        ["Cycle count", show(m.cycle_count)],
      ]}
    />
  );
}

// ------------------------------------------------------------------ 5. actions

const ACTION_STATUS: Record<string, { tone: Tone; hint: string }> = {
  pending: { tone: "amber", hint: "started, no outcome recorded yet" },
  completed: { tone: "green", hint: "finished successfully" },
  failed: { tone: "red", hint: "finished with a failure" },
};

export function ActionsSection({ result }: { result: Loaded<RunAction[]> }) {
  const note = "The supervisor's tool calls, OLDEST FIRST. An action is recorded when it starts; only the status says how it ended.";
  if (!result.ok) {
    return (
      <Section id="actions" title="Actions" note={note}>
        <SectionUnavailable what="Actions" error={result.error} />
      </Section>
    );
  }
  const actions = result.data;
  return (
    <Section id="actions" title={`Actions (${actions.length})`} note={note}>
      {actions.length === 0 ? (
        <Empty>The supervisor has not taken any action yet.</Empty>
      ) : (
        <ul className="space-y-3 text-sm">
          {actions.map((action) => {
            const status = ACTION_STATUS[action.status];
            return (
              <li key={action.id} className="rounded border border-slate-200 p-3">
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                  <span className="font-mono font-medium">{action.action_type}</span>
                  <Badge tone={status?.tone ?? "slate"}>{action.status}</Badge>
                  <span className="text-xs text-slate-500">{status?.hint ?? "unrecognised status"}</span>
                </div>
                <p className="mt-1 text-xs text-slate-500">
                  started {formatTimestamp(action.created_at)} · {action.completed_at ? `finished ${formatTimestamp(action.completed_at)}` : "not finished"} ·{" "}
                  <span className="font-mono">{shortId(action.id)}</span>
                </p>
                {action.reasoning && (
                  <p className="mt-2">
                    <span className="text-slate-500">Why: </span>
                    {action.reasoning}
                  </p>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Section>
  );
}

// ------------------------------------------------------------------ 6. tool executions

const TOOL_STATUS: Record<string, { tone: Tone; hint: string }> = {
  pending: { tone: "amber", hint: "started, no result recorded yet" },
  success: { tone: "green", hint: "the tool reported success" },
  failed: { tone: "red", hint: "the tool failed" },
};

export function ToolExecutionsSection({ result }: { result: Loaded<ToolExecution[]> }) {
  const note = "What each tool was called with and what it returned, OLDEST FIRST.";
  if (!result.ok) {
    return (
      <Section id="tools" title="Tool executions" note={note}>
        <SectionUnavailable what="Tool executions" error={result.error} />
      </Section>
    );
  }
  const executions = result.data;
  return (
    <Section id="tools" title={`Tool executions (${executions.length})`} note={note}>
      {executions.length === 0 ? (
        <Empty>No tool has been executed yet.</Empty>
      ) : (
        <ul className="space-y-3 text-sm">
          {executions.map((execution) => {
            const status = TOOL_STATUS[execution.status];
            const failed = execution.status === "failed";
            return (
              <li key={execution.id} className={`rounded border p-3 ${failed ? "border-red-200 bg-red-50/40" : "border-slate-200"}`}>
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                  <span className="font-mono font-medium">{execution.tool_name}</span>
                  <Badge tone={status?.tone ?? "slate"}>{execution.status}</Badge>
                  <span className="text-xs text-slate-500">{status?.hint ?? "unrecognised status"}</span>
                </div>
                <p className="mt-1 text-xs text-slate-500">
                  started {formatTimestamp(execution.started_at)} · {execution.completed_at ? `finished ${formatTimestamp(execution.completed_at)}` : "not finished"} · execution{" "}
                  <span className="font-mono">{shortId(execution.id)}</span> · action <span className="font-mono">{shortId(execution.action_id)}</span>
                </p>
                {execution.error && (
                  <p className="mt-2 text-red-800">
                    <span className="font-medium">Error: </span>
                    {execution.error}
                  </p>
                )}
                <details className="mt-2">
                  <summary className="cursor-pointer text-blue-700">Input and result</summary>
                  <div className="mt-2 grid gap-3 md:grid-cols-2">
                    <div>
                      <p className="mb-1 text-xs font-medium text-slate-500">Input</p>
                      <Json value={execution.input} />
                    </div>
                    <div>
                      <p className="mb-1 text-xs font-medium text-slate-500">Result</p>
                      {execution.result === null ? <p className="text-xs text-slate-500">No result recorded.</p> : <Json value={execution.result} />}
                    </div>
                  </div>
                </details>
              </li>
            );
          })}
        </ul>
      )}
    </Section>
  );
}

// ------------------------------------------------------------------ 7. final output

export function FinalOutputSection({ run, result }: { run: Run; result: Loaded<FinalOutputResponse> }) {
  if (!result.ok) {
    return (
      <Section id="final-output" title="Final output">
        <SectionUnavailable what="Final output" error={result.error} />
      </Section>
    );
  }
  const output = result.data.final_output;
  if (output === null || typeof output !== "object") {
    const active = ["starting", "running", "active"].includes(run.status);
    return (
      <Section id="final-output" title="Final output">
        <Empty>
          {active
            ? "Final output not available yet. It is produced when the run completes."
            : run.status === "completed"
              ? "No final output is recorded for this completed run."
              : `No final output was recorded for this ${run.status} run.`}
        </Empty>
      </Section>
    );
  }
  return (
    <Section
      id="final-output"
      title="Final output"
      tone="final"
      note={`The supervisor's end-of-run report, produced when the run completed${result.data.created_at ? ` (${formatTimestamp(result.data.created_at)})` : ""}.`}
    >
      <Dl
        rows={[
          ["Summary", output.summary ? <span key="sum" className="text-base font-medium">{show(output.summary)}</span> : "—"],
          ["Key actions", <StringList key="a" items={output.key_actions} empty="none" />],
          ["Key learnings", <StringList key="l" items={output.key_learnings} empty="none" />],
          ["Recommendations", <StringList key="r" items={output.recommendations} empty="none" />],
          [
            "Source",
            output.source ? (
              <span key="src">
                <Badge tone={output.source === "llm" ? "green" : "amber"}>{output.source}</Badge>{" "}
                <span className="text-xs text-slate-500">
                  {output.source === "fallback" ? "written by the workflow itself because generating it with the LLM failed" : output.source === "llm" ? "written by the LLM" : ""}
                </span>
              </span>
            ) : (
              "—"
            ),
          ],
        ]}
      />
    </Section>
  );
}
