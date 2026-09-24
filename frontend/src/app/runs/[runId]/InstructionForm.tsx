"use client";

import { useRouter } from "next/navigation";
import { useActionState } from "react";
import { Field, inputClass } from "@/components/Field";
import { FormAlert } from "@/components/FormAlert";
import { IDLE, type InstructionFormState } from "@/components/formState";
import { SubmitButton } from "@/components/SubmitButton";
import { addInstructionAction } from "./actions";

export function InstructionForm({ runId }: { runId: string }) {
  const [state, formAction] = useActionState<InstructionFormState, FormData>(addInstructionAction, IDLE);
  const router = useRouter();
  // A fresh form per result: React resets a form to the values it was MOUNTED with, so remounting keeps the
  // typed text after an error and clears it after a success.
  const formKey = state.status === "success" ? state.sentAt : state.status === "error" ? (state.at ?? "error") : "idle";

  return (
    <div className="mt-4 border-t border-slate-100 pt-4">
      {state.status === "success" && (
        <div role="status" className="mb-4 rounded-lg border border-green-200 bg-green-50 p-4 text-sm text-green-900">
          <p className="font-medium">Instruction added to this run.</p>
          <p className="mt-1">
            “{state.instruction}” was accepted by the workflow, which records it and normally wakes the supervisor to
            re-evaluate the order with it (if the run is paused, reasoning stays paused until it is resumed).
            {state.visible ? (
              " It is listed above."
            ) : (
              <>
                {" "}
                The workflow records it in a moment:{" "}
                <button type="button" onClick={() => router.refresh()} className="text-blue-700 underline">
                  refresh this page
                </button>{" "}
                to see it listed.
              </>
            )}
          </p>
        </div>
      )}
      {state.status === "error" && <FormAlert failure={state} />}

      <form key={formKey} action={formAction} className="space-y-3">
        <input type="hidden" name="run_id" value={runId} />
        <Field
          label="Add an instruction for this run"
          htmlFor="instruction"
          required
          hint={'Examples: "If shipment is delayed, escalate immediately." · "Do not contact the customer without human review."'}
        >
          <textarea
            id="instruction"
            name="instruction"
            required
            rows={2}
            defaultValue={state.status === "error" ? (state.values.instruction as string) : ""}
            className={inputClass}
            placeholder="For this order, prioritize speed over cost."
          />
        </Field>
        <SubmitButton label="Add instruction" pendingLabel="Adding…" />
      </form>
    </div>
  );
}
