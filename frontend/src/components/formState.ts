import type { FailureView } from "@/api/messages";

/** Submitted values, echoed back after a failed submission so nothing the user typed is lost. */
export type FormValues = Record<string, string | string[]>;

export type FormState = { status: "idle" } | ({ status: "error"; values: FormValues; at?: string } & FailureView);

export const IDLE: FormState = { status: "idle" };

/** Event injection can also succeed in place (the user stays on the run page). */
export type EventFormState = FormState | { status: "success"; eventType: string; sentAt: string };
