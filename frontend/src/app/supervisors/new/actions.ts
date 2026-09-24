"use server";

import { redirect } from "next/navigation";
import { describeFailure } from "@/api/messages";
import { createSupervisor } from "@/api/supervisors";
import type { SupervisorCreateBody } from "@/api/types";
import { EVENT_TYPES } from "@/api/vocabulary";
import type { FormState, FormValues } from "@/components/formState";

function text(formData: FormData, name: string): string {
  const value = formData.get(name);
  return typeof value === "string" ? value : "";
}

function readValues(formData: FormData): FormValues {
  const values: FormValues = {
    name: text(formData, "name"),
    description: text(formData, "description"),
    instructions: text(formData, "instructions"),
    enabled_tools: formData.getAll("enabled_tools").map(String),
    important_event_types: formData.getAll("important_event_types").map(String),
    min_wake: text(formData, "min_wake"),
    default_wake: text(formData, "default_wake"),
    max_wake: text(formData, "max_wake"),
    terminal_statuses: text(formData, "terminal_statuses"),
  };
  for (const event of EVENT_TYPES) values[`status_${event}`] = text(formData, `status_${event}`);
  return values;
}

function invalid(message: string, values: FormValues): FormState {
  return { status: "error", title: "Please fix the form", message, code: null, httpStatus: null, values };
}

/**
 * Creates a supervisor through POST /api/supervisors, then continues to "Start run" with it selected.
 * The checks below only save a round trip for obvious mistakes; the backend stays authoritative.
 */
export async function createSupervisorAction(_previous: FormState, formData: FormData): Promise<FormState> {
  const values = readValues(formData);
  const str = (key: string) => (values[key] as string).trim();
  const list = (key: string) => values[key] as string[];

  if (!str("name")) return invalid("Name is required.", values);
  if (!str("instructions")) return invalid("The base instruction is required.", values);
  if (list("enabled_tools").length === 0) return invalid("Select at least one tool.", values);

  const intervals = [str("min_wake"), str("default_wake"), str("max_wake")].map((v) => (v === "" ? NaN : Number(v)));
  if (!intervals.every((n) => Number.isInteger(n))) return invalid("Wake intervals must be whole numbers of minutes.", values);
  const [min, standard, max] = intervals;

  const terminal = str("terminal_statuses")
    .split(/[,\n]/)
    .map((s) => s.trim())
    .filter(Boolean);
  if (terminal.length === 0) {
    return invalid("Give at least one terminal order status, otherwise runs can never finish (for example: delivered, cancelled).", values);
  }

  const statusByEvent: Record<string, string> = {};
  for (const event of EVENT_TYPES) {
    const status = str(`status_${event}`);
    if (status) statusByEvent[event] = status;
  }

  const body: SupervisorCreateBody = {
    name: str("name"),
    description: str("description") || null,
    instructions: str("instructions"),
    wake_policy: { important_event_types: list("important_event_types") },
    enabled_tools: list("enabled_tools"),
    default_wake_interval_minutes: standard,
    min_wake_interval_minutes: min,
    max_wake_interval_minutes: max,
    terminal_order_statuses: terminal,
    order_status_by_event: statusByEvent,
  };

  let created;
  try {
    created = await createSupervisor(body);
  } catch (error) {
    return { status: "error", ...describeFailure(error, "supervisor"), values };
  }
  if (!created || typeof created.id !== "string" || typeof created.version !== "number") {
    return {
      status: "error",
      title: "Unexpected response from the backend",
      message: "The backend answered, but the response does not look like a supervisor. Check the Supervisors page before retrying.",
      code: "MALFORMED_RESPONSE",
      httpStatus: null,
      values,
    };
  }
  // redirect() throws by design, so it stays outside the try/catch.
  redirect(`/runs/new?supervisorId=${encodeURIComponent(created.id)}&created=${created.version}`);
}
