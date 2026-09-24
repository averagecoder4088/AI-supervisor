import type { FailureView } from "@/api/messages";

/** Submitted values, echoed back after a failed submission so nothing the user typed is lost. */
export type FormValues = Record<string, string | string[]>;

export type FormState = { status: "idle" } | ({ status: "error"; values: FormValues } & FailureView);

export const IDLE: FormState = { status: "idle" };
