"use client";

import { useActionState } from "react";
import { Field, inputClass } from "@/components/Field";
import { FormAlert } from "@/components/FormAlert";
import { IDLE, type FormState } from "@/components/formState";
import { SubmitButton } from "@/components/SubmitButton";
import { startRunAction } from "./actions";

export interface SupervisorOption {
  id: string;
  label: string;
}

export function StartRunForm({ options, selectedId }: { options: SupervisorOption[]; selectedId: string }) {
  const [state, formAction] = useActionState<FormState, FormData>(startRunAction, IDLE);
  const values = state.status === "error" ? state.values : null;
  const str = (key: string, fallback: string) => (values ? ((values[key] as string) ?? "") : fallback);

  return (
    <form action={formAction} className="space-y-6">
      {state.status === "error" && <FormAlert failure={state} />}

      <section className="space-y-4 rounded-lg border border-slate-200 bg-white p-5">
        <Field label="Order ID" htmlFor="order_id" required hint="Each order has exactly one run, and one Temporal workflow named order-<order ID>.">
          <input id="order_id" name="order_id" required defaultValue={str("order_id", "")} className={inputClass} placeholder="ORDER-12345" />
        </Field>

        <Field label="Supervisor" htmlFor="supervisor_id" hint="The run uses this supervisor version exactly as it is now; later versions never change a running run.">
          <select id="supervisor_id" name="supervisor_id" defaultValue={str("supervisor_id", selectedId)} className={inputClass}>
            <option value="">{options.length === 0 ? "No supervisors known yet" : "Select a supervisor…"}</option>
            {options.map((option) => (
              <option key={option.id} value={option.id}>
                {option.label}
              </option>
            ))}
          </select>
        </Field>
        <Field
          label="Or paste a supervisor ID"
          htmlFor="pasted_supervisor_id"
          hint="The backend has no supervisor list, so the dropdown only shows supervisors from existing runs and the one you just created. A pasted ID takes priority."
        >
          <input id="pasted_supervisor_id" name="pasted_supervisor_id" defaultValue={str("pasted_supervisor_id", "")} className={`${inputClass} font-mono`} placeholder="optional" />
        </Field>
      </section>

      <section className="rounded-lg border border-slate-200 bg-white p-5">
        <Field
          label="Run-specific instructions"
          htmlFor="run_instructions"
          hint="Optional, one per line. These apply to THIS order only and are added to the supervisor's base instruction (which stays unchanged), for example: If the shipment is delayed, escalate immediately."
        >
          <textarea id="run_instructions" name="run_instructions" rows={4} defaultValue={str("run_instructions", "")} className={inputClass} />
        </Field>
      </section>

      <SubmitButton label="Start run" pendingLabel="Starting…" />
    </form>
  );
}
