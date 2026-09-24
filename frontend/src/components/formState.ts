import type { FailureView } from "@/api/messages";
import type { ControlName } from "@/api/vocabulary";

/** Submitted values, echoed back after a failed submission so nothing the user typed is lost. */
export type FormValues = Record<string, string | string[]>;

export type FormState = { status: "idle" } | ({ status: "error"; values: FormValues; at?: string } & FailureView);

export const IDLE: FormState = { status: "idle" };

/** Event injection can also succeed in place (the user stays on the run page). */
export type EventFormState = FormState | { status: "success"; eventType: string; sentAt: string };

/**
 * Adding an instruction also succeeds in place. `visible` says whether the workflow had already recorded
 * it (so the refreshed run page lists it) by the time the action returned.
 */
export type InstructionFormState = FormState | { status: "success"; instruction: string; sentAt: string; visible: boolean };

/**
 * A human control was accepted. The observed fields come from ONE read after the request (no polling) and
 * are null when that read failed; a Signal is applied a moment after it is accepted.
 */
export type ControlFormState =
  | FormState
  | {
      status: "success";
      control: ControlName;
      sentAt: string;
      /** The workflow's own state from GET /status (pause, resume, interrupt). */
      workflowState: string | null;
      interruptCount: number | null;
      /** The run's status from GET /runs/{id} (terminate). */
      runStatus: string | null;
    };
