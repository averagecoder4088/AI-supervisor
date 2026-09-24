"use client";

import { useRouter } from "next/navigation";
import { useActionState, useState } from "react";
import { useFormStatus } from "react-dom";
import type { ControlName } from "@/api/vocabulary";
import { FormAlert } from "@/components/FormAlert";
import { IDLE, type ControlFormState } from "@/components/formState";
import { formatTimestamp } from "@/format";
import { controlAction } from "./actions";
import type { ControlMode } from "./controlMode";

const INFO: Record<ControlName, { label: string; pending: string; text: string }> = {
  pause: {
    label: "Pause",
    pending: "Pausing…",
    text: "Keep the workflow alive but stop supervisor reasoning. Events are still recorded and can be evaluated after Resume.",
  },
  resume: {
    label: "Resume",
    pending: "Resuming…",
    text: "Allow supervisor reasoning to continue: the supervisor re-evaluates the order.",
  },
  interrupt: {
    label: "Interrupt",
    pending: "Interrupting…",
    text: "Stop the current reasoning cycle without terminating the workflow. If an LLM decision is in flight, it is discarded; a tool that already started is allowed to finish and is recorded.",
  },
  terminate: {
    label: "Terminate",
    pending: "Terminating…",
    text: "Hard-stop this workflow. This cannot be undone. It is not Pause and not Interrupt: the workflow does not continue afterwards.",
  },
};

const OFFERED: Record<"running" | "paused" | "unavailable", ControlName[]> = {
  running: ["pause", "interrupt", "terminate"],
  paused: ["resume", "interrupt", "terminate"],
  // The state is not known, so which of Pause / Resume applies is not known either: show all four, disabled.
  unavailable: ["pause", "resume", "interrupt", "terminate"],
};

const BUTTON = "rounded border px-4 py-2 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50";
const NEUTRAL = `${BUTTON} border-slate-300 bg-white text-slate-800 hover:bg-slate-50`;
const DANGER = `${BUTTON} border-red-700 bg-red-700 text-white hover:bg-red-800`;

/**
 * Always rendered (even for a run that offers no controls) so the outcome banner survives the state change it
 * causes: after a successful Terminate the page re-renders with the run terminated, and the banner must stay.
 * `mode` "inactive" = the run's own status is not active.
 */
export function HumanControls({ runId, mode, runStatus }: { runId: string; mode: ControlMode | "inactive"; runStatus: string }) {
  const [state, formAction] = useActionState<ControlFormState, FormData>(controlAction, IDLE);
  const router = useRouter();
  // A fresh form per result, so the terminate confirmation closes and nothing stale is left in it.
  const formKey = state.status === "success" ? state.sentAt : state.status === "error" ? (state.at ?? "error") : "idle";

  return (
    <div>
      {state.status === "success" && <Outcome state={state} />}
      {state.status === "error" && <FormAlert failure={state} />}
      {mode === "finishing" && (
        <p className="text-sm text-slate-600">
          The order has reached a terminal status and the workflow is completing, so human controls are no longer offered.
        </p>
      )}
      {mode === "closed" && (
        <p className="text-sm text-slate-600">
          The workflow is no longer running, so it no longer accepts human controls. Refresh this page to see the run&apos;s
          final status.
        </p>
      )}
      {mode === "inactive" && (
        <p className="text-sm text-slate-600">
          This run is <span className="font-medium">{runStatus}</span> and is no longer accepting human controls. Only an
          active run can be paused, resumed, interrupted or terminated.
        </p>
      )}
      {mode === "unavailable" && (
        <p role="note" className="mb-3 rounded bg-amber-50 p-3 text-sm text-amber-900">
          Unable to determine the workflow state. Refresh and try again.{" "}
          <button type="button" onClick={() => router.refresh()} className="text-blue-700 underline">
            Refresh this page
          </button>
        </p>
      )}
      {(mode === "running" || mode === "paused" || mode === "unavailable") && (
        <form key={formKey} action={formAction}>
          <input type="hidden" name="run_id" value={runId} />
          <Buttons offered={OFFERED[mode]} disabled={mode === "unavailable"} />
        </form>
      )}
    </div>
  );
}

/** The buttons. Must sit inside the form: useFormStatus tells which one was pressed and disables all of them. */
function Buttons({ offered, disabled }: { offered: ControlName[]; disabled: boolean }) {
  const { pending: inFlight, data } = useFormStatus();
  const pending = inFlight || disabled; // `disabled`: the workflow state is unknown, nothing may be sent
  const pressed = inFlight ? String(data?.get("control") ?? "") : "";
  const [confirming, setConfirming] = useState(false);

  return (
    <ul className="space-y-3">
      {offered.map((control) => {
        const info = INFO[control];
        return (
          <li key={control} className="flex flex-wrap items-start gap-x-4 gap-y-2">
            <div className="w-32 shrink-0">
              {control === "terminate" && !confirming ? (
                <button type="button" disabled={pending} onClick={() => setConfirming(true)} className={DANGER}>
                  Terminate…
                </button>
              ) : control === "terminate" ? (
                <span className="inline-block py-2 text-sm font-medium text-red-800">Confirm below</span>
              ) : (
                <button type="submit" name="control" value={control} disabled={pending} className={NEUTRAL}>
                  {pressed === control ? info.pending : info.label}
                </button>
              )}
            </div>
            <p className="min-w-0 flex-1 basis-64 py-2 text-sm text-slate-600">{info.text}</p>
            {control === "terminate" && confirming && (
              <div role="group" aria-label="Confirm terminate" className="basis-full rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-900">
                <p className="font-medium">Terminate this workflow?</p>
                <p className="mt-1">
                  This hard-stops the workflow on Temporal and cannot be undone. Use Pause or Interrupt instead if you only
                  want the supervisor to stop reasoning.
                </p>
                <input type="hidden" name="confirm" value="yes" />
                <div className="mt-3 flex gap-3">
                  <button type="submit" name="control" value="terminate" disabled={pending} className={DANGER}>
                    {pressed === "terminate" ? info.pending : "Yes, terminate this workflow"}
                  </button>
                  <button type="button" disabled={pending} onClick={() => setConfirming(false)} className={NEUTRAL}>
                    Cancel
                  </button>
                </div>
              </div>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/** What the backend reported after an accepted request. "Accepted" is not "applied": say what was observed. */
function Outcome({ state }: { state: Extract<ControlFormState, { status: "success" }> }) {
  const router = useRouter();
  const { control, workflowState, interruptCount, runStatus } = state;
  const notYet =
    (control === "pause" && workflowState !== null && workflowState !== "paused") ||
    (control === "resume" && workflowState === "paused") ||
    (control === "terminate" && runStatus !== null && runStatus !== "terminated");
  const unread = control === "terminate" ? runStatus === null : workflowState === null;

  return (
    <div role="status" className="mb-4 rounded-lg border border-green-200 bg-green-50 p-4 text-sm text-green-900">
      <p className="font-medium">
        {INFO[control].label} requested ({formatTimestamp(state.sentAt)}).
      </p>
      <p className="mt-1">
        The backend accepted it: it was delivered to {control === "terminate" ? "Temporal" : "the workflow"}. Accepted is
        not the same as applied.
        {control === "terminate" && runStatus !== null && <> The backend now reports the run as <span className="font-medium">{runStatus}</span>.</>}
        {control !== "terminate" && workflowState !== null && (
          <> The workflow reports its state as <span className="font-medium">{workflowState}</span>.</>
        )}
        {control === "interrupt" && interruptCount !== null && <> Interrupt count: {interruptCount}.</>}
        {control === "interrupt" && (
          <> If a reasoning cycle was in progress, its LLM decision is discarded; a tool that had already started finishes and is recorded. The workflow stays alive.</>
        )}
        {unread && <> The resulting state could not be read just now.</>}
        {(notYet || unread) && (
          <>
            {" "}
            <button type="button" onClick={() => router.refresh()} className="text-blue-700 underline">
              Refresh this page
            </button>{" "}
            to see the current state.
          </>
        )}
      </p>
    </div>
  );
}
