import { ApiError } from "./client";

/** What a form shows after a failed submission. Plain data: it crosses the Server Action boundary. */
export interface FailureView {
  title: string;
  message: string;
  code: string | null;
  httpStatus: number | null;
}

type Context = "supervisor" | "run" | "event" | "instruction";

/**
 * Turn any failure into user-facing text. Known backend error codes get an explanation of what
 * happened and what to do; the backend's own message is kept because it is the authoritative
 * validation feedback. Stack traces and raw exceptions are never shown.
 */
export function describeFailure(error: unknown, context: Context): FailureView {
  if (!(error instanceof ApiError)) {
    return { title: "Unexpected error", message: "Something unexpected went wrong. Nothing was created.", code: null, httpStatus: null };
  }
  const { code, status } = error;
  const base = { code, httpStatus: status };

  switch (code) {
    case "BACKEND_UNREACHABLE":
      return { ...base, title: "Backend unavailable", message: error.message };
    case "BACKEND_TIMEOUT":
      return {
        ...base,
        title: "The backend did not answer in time",
        message: `${error.message} The request may still have been processed: check the Dashboard before submitting again.`,
      };
    case "MALFORMED_RESPONSE":
      return { ...base, title: "Unexpected response from the backend", message: error.message };
    case "VALIDATION_ERROR":
      return { ...base, title: "The backend rejected the input", message: error.message };
    case "SUPERVISOR_VERSION_CONFLICT":
      return {
        ...base,
        title: "Conflict while creating the supervisor",
        message: "Another request created this supervisor version at the same moment. Submit again to get the next version.",
      };
    case "SUPERVISOR_NOT_FOUND":
      return { ...base, title: "Supervisor not found", message: "No supervisor exists with that ID. Create one first, or check the ID." };
    case "RUN_ALREADY_EXISTS":
      return {
        ...base,
        title: "A run for this order already exists",
        message: `${error.message}. Each order has exactly one run (and a run that failed to start keeps its order ID). Use a different order ID.`,
      };
    case "WORKFLOW_ALREADY_STARTED":
      return {
        ...base,
        title: "A workflow for this order already exists",
        message: `${error.message}. Use a different order ID.`,
      };
    case "RUN_NOT_FOUND":
      return { ...base, title: "Run not found", message: "The backend has no run with this ID, so nothing was sent." };
    case "RUN_NOT_ACTIVE":
      return {
        ...base,
        title: "The run is no longer active",
        message: `${error.message}. Only an active run can accept ${context === "instruction" ? "instructions" : "events"} (a completed, terminated or failed run cannot). Nothing was sent.`,
      };
    case "TEMPORAL_UNAVAILABLE":
      if (context === "event" || context === "instruction") {
        return {
          ...base,
          title: "Temporal is unavailable",
          message: `The backend cannot reach Temporal, so the ${context} was NOT delivered to the workflow. Start Temporal and try again.`,
        };
      }
      return {
        ...base,
        title: "Temporal is unavailable",
        message:
          "The run could not be started because the backend cannot reach Temporal. Start Temporal and the worker, then try again. " +
          "If the backend had already recorded this order, a retry may report that the order already exists.",
      };
    case "WORKFLOW_START_FAILED":
    case "RUN_STATE_UPDATE_FAILED":
      return { ...base, title: "The backend could not finish starting the run", message: error.message };
  }
  if (status !== null && status >= 500) {
    return { ...base, title: "Backend error", message: `The backend reported an internal error: ${error.message}` };
  }
  if (status !== null && status >= 400) {
    return {
      ...base,
      title: context === "supervisor"
          ? "The supervisor was not created"
          : context === "event"
            ? "The event was not sent"
            : context === "instruction"
              ? "The instruction was not added"
              : "The run was not started",
      message: error.message,
    };
  }
  return { ...base, title: "Request failed", message: error.message };
}
