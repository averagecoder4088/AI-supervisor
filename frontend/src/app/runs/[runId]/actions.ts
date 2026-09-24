"use server";

import { revalidatePath } from "next/cache";
import { describeFailure } from "@/api/messages";
import { injectEvent } from "@/api/runs";
import { EVENT_TYPES } from "@/api/vocabulary";
import type { EventFormState, FormValues } from "@/components/formState";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function text(formData: FormData, name: string): string {
  const value = formData.get(name);
  return typeof value === "string" ? value : "";
}

/**
 * Sends an event into a run's workflow: browser -> this Server Action -> FastAPI
 * (POST /api/runs/{id}/events) -> Temporal Signal. The user stays on the run page.
 * The checks below only save a round trip; the backend stays authoritative.
 */
export async function injectEventAction(_previous: EventFormState, formData: FormData): Promise<EventFormState> {
  const values: FormValues = {
    run_id: text(formData, "run_id"),
    event_type: text(formData, "event_type"),
    payload: text(formData, "payload"),
  };
  const fail = (message: string): EventFormState => ({
    status: "error",
    title: "Please fix the form",
    message,
    code: null,
    httpStatus: null,
    values,
    at: new Date().toISOString(),
  });

  const runId = (values.run_id as string).trim();
  const eventType = (values.event_type as string).trim();
  const payloadText = (values.payload as string).trim();

  if (!UUID.test(runId)) return fail("This page's run ID is not valid, so nothing was sent.");
  if (!eventType) return fail("Select an event type.");
  if (!(EVENT_TYPES as readonly string[]).includes(eventType)) {
    return fail(`Unknown event type "${eventType}". Choose one of the listed event types.`);
  }

  let payload: Record<string, unknown> = {};
  if (payloadText) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(payloadText);
    } catch {
      return fail('The details are not valid JSON. Use a JSON object such as {"reason": "card_declined"}, or leave the field empty.');
    }
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      return fail("The details must be a JSON object in curly braces (not a list or a single value), or empty.");
    }
    payload = parsed as Record<string, unknown>;
  }

  let accepted;
  try {
    accepted = await injectEvent(runId, { event_type: eventType, payload });
  } catch (error) {
    return { status: "error", ...describeFailure(error, "event"), values, at: new Date().toISOString() };
  }
  if (!accepted || accepted.accepted !== true) {
    return {
      status: "error",
      title: "Unexpected response from the backend",
      message: "The backend answered, but did not confirm that it accepted the event. Check the run before sending it again.",
      code: "MALFORMED_RESPONSE",
      httpStatus: null,
      values,
      at: new Date().toISOString(),
    };
  }

  revalidatePath(`/runs/${runId}`); // re-render the run page (order status may have changed)
  return { status: "success", eventType, sentAt: new Date().toISOString() };
}
