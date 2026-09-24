"use server";

import { redirect } from "next/navigation";
import { describeFailure } from "@/api/messages";
import { createRun } from "@/api/runs";
import type { FormState, FormValues } from "@/components/formState";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function text(formData: FormData, name: string): string {
  const value = formData.get(name);
  return typeof value === "string" ? value : "";
}

/**
 * Starts a run through POST /api/runs (records the run and starts its Temporal workflow), then opens
 * the run page. The checks only save a round trip; the backend stays authoritative.
 */
export async function startRunAction(_previous: FormState, formData: FormData): Promise<FormState> {
  const values: FormValues = {
    order_id: text(formData, "order_id"),
    supervisor_id: text(formData, "supervisor_id"),
    pasted_supervisor_id: text(formData, "pasted_supervisor_id"),
    run_instructions: text(formData, "run_instructions"),
  };
  const fail = (message: string): FormState => ({
    status: "error",
    title: "Please fix the form",
    message,
    code: null,
    httpStatus: null,
    values,
    at: new Date().toISOString(),
  });

  const orderId = (values.order_id as string).trim();
  // A pasted ID wins over the dropdown, so a supervisor that is not listed can still be used.
  const supervisorId = (values.pasted_supervisor_id as string).trim() || (values.supervisor_id as string).trim();
  const instructions = (values.run_instructions as string)
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);

  if (!orderId) return fail("Order ID is required.");
  if (!supervisorId) return fail("Choose a supervisor, or paste a supervisor ID. Create a supervisor first if none exists.");
  if (!UUID.test(supervisorId)) return fail("The supervisor ID must be a UUID (for example 3f2b1c9e-8a4d-4c1e-9b7a-2d5e6f708192).");

  let run;
  try {
    run = await createRun({ order_id: orderId, supervisor_id: supervisorId, run_instructions: instructions });
  } catch (error) {
    return { status: "error", ...describeFailure(error, "run"), values, at: new Date().toISOString() };
  }
  if (!run || typeof run.id !== "string") {
    return {
      status: "error",
      title: "Unexpected response from the backend",
      message: "The backend answered, but the response does not look like a run. Check the Dashboard before retrying.",
      code: "MALFORMED_RESPONSE",
      httpStatus: null,
      values,
      at: new Date().toISOString(),
    };
  }
  redirect(`/runs/${encodeURIComponent(run.id)}?started=1`);
}
