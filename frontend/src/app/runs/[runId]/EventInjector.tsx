"use client";

import { useRouter } from "next/navigation";
import { useActionState, useRef, useState } from "react";
import { EVENT_PAYLOAD_EXAMPLES, EVENT_TYPES } from "@/api/vocabulary";
import { Field, inputClass } from "@/components/Field";
import { FormAlert } from "@/components/FormAlert";
import { IDLE, type EventFormState, type FormValues } from "@/components/formState";
import { SubmitButton } from "@/components/SubmitButton";
import { formatTimestamp } from "@/format";
import { injectEventAction } from "./actions";

export function EventInjector({ runId, workflowId }: { runId: string; workflowId: string }) {
  const [state, formAction] = useActionState<EventFormState, FormData>(injectEventAction, IDLE);
  const router = useRouter();
  const values = state.status === "error" ? state.values : null;
  // A fresh form per result: React resets a form after every action to the values it was MOUNTED with,
  // so remounting keeps the typed values after an error and clears them after a success.
  const formKey = state.status === "success" ? state.sentAt : state.status === "error" ? (state.at ?? "error") : "idle";

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-5">
      <h2 className="text-lg font-medium">Inject an event</h2>
      <p className="mt-1 mb-4 text-sm text-slate-600">
        Sends an order event into this run&apos;s running workflow <span className="font-mono">{workflowId}</span> (through
        the backend, as a Temporal Signal). The supervisor records every event; only events its wake policy marks
        important wake it immediately, and an event may also change the order status.
      </p>

      {state.status === "success" && (
        <div role="status" className="mb-4 rounded-lg border border-green-200 bg-green-50 p-4 text-sm text-green-900">
          <p className="font-medium">
            Event <span className="font-mono">{state.eventType}</span> sent to the workflow.
          </p>
          <p className="mt-1">
            The backend accepted it at {formatTimestamp(state.sentAt)}. Accepted means delivered to the workflow, which
            records it a moment later.{" "}
            <button type="button" onClick={() => router.refresh()} className="text-blue-700 underline">
              Refresh this page
            </button>{" "}
            to see the order status.
          </p>
        </div>
      )}
      {state.status === "error" && <FormAlert failure={state} />}

      <form key={formKey} action={formAction} className="space-y-4">
        <input type="hidden" name="run_id" value={runId} />
        <EventFields initial={values} />
        <SubmitButton label="Send event" pendingLabel="Sending…" />
      </form>
    </section>
  );
}

/** The event and details inputs. Its state (the example hint) restarts with each fresh form. */
function EventFields({ initial }: { initial: FormValues | null }) {
  const [selected, setSelected] = useState(initial ? (initial.event_type as string) : "");
  const payloadRef = useRef<HTMLTextAreaElement>(null);
  const example = EVENT_PAYLOAD_EXAMPLES[selected] ?? "";

  return (
    <>
      <Field label="Event type" htmlFor="event_type" required>
        <select
          id="event_type"
          name="event_type"
          required
          defaultValue={initial ? (initial.event_type as string) : ""}
          onChange={(event) => setSelected(event.target.value)}
          className={inputClass}
        >
          <option value="">Select an event…</option>
          {EVENT_TYPES.map((event) => (
            <option key={event} value={event}>
              {event}
            </option>
          ))}
        </select>
      </Field>
      <Field
        label="Details (JSON object, optional)"
        htmlFor="payload"
        hint={
          example
            ? `Example for ${selected}: ${example}`
            : 'Any JSON object, for example {"reason": "card_declined"}. Leave empty for no details.'
        }
      >
        <textarea
          ref={payloadRef}
          id="payload"
          name="payload"
          rows={3}
          defaultValue={initial ? (initial.payload as string) : ""}
          className={`${inputClass} font-mono`}
          placeholder={example || "{}"}
          spellCheck={false}
        />
        <button
          type="button"
          disabled={!example}
          onClick={() => {
            if (payloadRef.current) payloadRef.current.value = example;
          }}
          className="mt-2 text-sm text-blue-700 underline disabled:cursor-not-allowed disabled:text-slate-400 disabled:no-underline"
        >
          Use the example details
        </button>
      </Field>
    </>
  );
}
