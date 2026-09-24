"use client";

import { useActionState } from "react";
import {
  DEFAULT_IMPORTANT_EVENTS,
  DEFAULT_STATUS_BY_EVENT,
  DEFAULT_TERMINAL_STATUSES,
  DEFAULT_WAKE_MINUTES,
  EVENT_TYPES,
  TOOLS,
} from "@/api/vocabulary";
import { Field, inputClass } from "@/components/Field";
import { FormAlert } from "@/components/FormAlert";
import { IDLE, type FormState } from "@/components/formState";
import { SubmitButton } from "@/components/SubmitButton";
import { createSupervisorAction } from "./actions";

export function SupervisorForm() {
  const [state, formAction] = useActionState<FormState, FormData>(createSupervisorAction, IDLE);
  const values = state.status === "error" ? state.values : null;
  const str = (key: string, fallback: string) => (values ? ((values[key] as string) ?? "") : fallback);
  const list = (key: string, fallback: string[]) => (values ? ((values[key] as string[]) ?? []) : fallback);
  const tools = list("enabled_tools", TOOLS.map((t) => t.name));
  const important = list("important_event_types", DEFAULT_IMPORTANT_EVENTS);

  return (
    <form action={formAction} className="space-y-8">
      {state.status === "error" && <FormAlert failure={state} />}

      <section className="space-y-4 rounded-lg border border-slate-200 bg-white p-5">
        <h2 className="text-lg font-medium">Identity</h2>
        <Field label="Name" htmlFor="name" required hint="Creating a supervisor with an existing name creates the next version (versions are immutable).">
          <input id="name" name="name" required defaultValue={str("name", "")} className={inputClass} placeholder="Shipment supervisor" />
        </Field>
        <Field label="Description" htmlFor="description">
          <input id="description" name="description" defaultValue={str("description", "")} className={inputClass} />
        </Field>
        <Field
          label="Base instruction"
          htmlFor="instructions"
          required
          hint="How this supervisor behaves for EVERY run. Instructions for one specific order are added when you start a run."
        >
          <textarea
            id="instructions"
            name="instructions"
            required
            rows={4}
            defaultValue={str("instructions", "")}
            className={inputClass}
            placeholder="Monitor the order until it is delivered. Escalate serious shipment delays and keep the customer informed."
          />
        </Field>
      </section>

      <section className="rounded-lg border border-slate-200 bg-white p-5">
        <h2 className="text-lg font-medium">Available tools</h2>
        <p className="mb-3 text-sm text-slate-500">
          The fixed tool set. The supervisor may use at most one tool per reasoning cycle, and only those selected here.
        </p>
        <div className="space-y-2">
          {TOOLS.map((tool) => (
            <label key={tool.name} className="flex items-start gap-3 rounded border border-slate-200 p-3 text-sm">
              <input type="checkbox" name="enabled_tools" value={tool.name} defaultChecked={tools.includes(tool.name)} className="mt-1" />
              <span>
                <span className="font-mono text-xs">{tool.name}</span>{" "}
                <span className={`ml-1 rounded px-1.5 py-0.5 text-xs ${tool.actsOnTheWorld ? "bg-amber-100 text-amber-800" : "bg-slate-100 text-slate-600"}`}>
                  {tool.actsOnTheWorld ? "acts on the order" : "read-only"}
                </span>
                <span className="block text-slate-600">{tool.description}</span>
              </span>
            </label>
          ))}
        </div>
      </section>

      <section className="space-y-4 rounded-lg border border-slate-200 bg-white p-5">
        <h2 className="text-lg font-medium">Wake behaviour</h2>
        <div>
          <p className="mb-1 text-sm font-medium text-slate-800">Events that wake the supervisor immediately</p>
          <p className="mb-2 text-xs text-slate-500">Other events are recorded but do not wake it; it reviews them at its next scheduled wake-up.</p>
          <div className="grid grid-cols-1 gap-x-6 gap-y-1 sm:grid-cols-2">
            {EVENT_TYPES.map((event) => (
              <label key={event} className="flex items-center gap-2 text-sm">
                <input type="checkbox" name="important_event_types" value={event} defaultChecked={important.includes(event)} />
                <span className="font-mono text-xs">{event}</span>
              </label>
            ))}
          </div>
        </div>
        <div className="grid grid-cols-3 gap-4">
          <Field label="Minimum wake (min)" htmlFor="min_wake" required>
            <input id="min_wake" name="min_wake" type="number" min={1} step={1} required defaultValue={str("min_wake", String(DEFAULT_WAKE_MINUTES.min))} className={inputClass} />
          </Field>
          <Field label="Default wake (min)" htmlFor="default_wake" required>
            <input id="default_wake" name="default_wake" type="number" min={1} step={1} required defaultValue={str("default_wake", String(DEFAULT_WAKE_MINUTES.default))} className={inputClass} />
          </Field>
          <Field label="Maximum wake (min)" htmlFor="max_wake" required>
            <input id="max_wake" name="max_wake" type="number" min={1} step={1} required defaultValue={str("max_wake", String(DEFAULT_WAKE_MINUTES.max))} className={inputClass} />
          </Field>
        </div>
        <p className="text-xs text-slate-500">Scheduled review interval: the model may ask for a different one, kept within min and max. Must satisfy 0 &lt; min ≤ default ≤ max.</p>
      </section>

      <section className="space-y-4 rounded-lg border border-slate-200 bg-white p-5">
        <h2 className="text-lg font-medium">When a run ends</h2>
        <Field
          label="Terminal order statuses"
          htmlFor="terminal_statuses"
          required
          hint="Comma-separated. When the order reaches one of these, the supervisor writes its final report and the run completes."
        >
          <input id="terminal_statuses" name="terminal_statuses" required defaultValue={str("terminal_statuses", DEFAULT_TERMINAL_STATUSES)} className={inputClass} />
        </Field>
        <details>
          <summary className="cursor-pointer text-sm font-medium text-slate-800">Advanced: order status set by each event</summary>
          <p className="my-2 text-xs text-slate-500">
            An event changes the order status only if it is mapped here (blank = no change). Without a mapping to a terminal status, a run cannot finish through events.
          </p>
          <div className="grid grid-cols-1 gap-x-6 gap-y-2 sm:grid-cols-2">
            {EVENT_TYPES.map((event) => (
              <label key={event} className="flex items-center gap-2 text-sm">
                <span className="w-56 font-mono text-xs">{event}</span>
                <input
                  name={`status_${event}`}
                  defaultValue={str(`status_${event}`, DEFAULT_STATUS_BY_EVENT[event] ?? "")}
                  className={inputClass}
                  aria-label={`Order status set by ${event}`}
                />
              </label>
            ))}
          </div>
        </details>
      </section>

      <SubmitButton label="Create supervisor" pendingLabel="Creating…" />
    </form>
  );
}
