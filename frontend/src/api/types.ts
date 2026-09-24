// Response shapes of the existing FastAPI backend (backend/app/api/schemas.py).
// Datetimes arrive as ISO-8601 strings. Only what this frontend reads is listed.

export interface RunInstruction {
  text?: string;
  added_at?: string | null;
}

/** RunOut: GET /api/runs, GET /api/runs/{id}. */
export interface Run {
  id: string;
  order_id: string;
  supervisor_id: string;
  supervisor_version: number;
  /** Application lifecycle: starting | running | completed | terminated | failed (legacy: active). */
  status: string;
  /** Business status of the order (null until the first status-changing event). */
  order_status: string | null;
  run_instructions: RunInstruction[];
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
}

/** SupervisorOut: GET /api/supervisors/{id}. */
export interface Supervisor {
  id: string;
  name: string;
  description: string | null;
  instructions: string;
  enabled_tools: string[];
  default_wake_interval_minutes: number;
  min_wake_interval_minutes: number;
  max_wake_interval_minutes: number;
  terminal_order_statuses: string[];
  version: number;
  created_at: string;
  updated_at: string;
}

/** Every backend error body: {"error": message, "code": CODE}. */
export interface ApiErrorBody {
  error: string;
  code: string;
}

/** SupervisorCreate: POST /api/supervisors (the backend rejects any extra field). */
export interface SupervisorCreateBody {
  name: string;
  description: string | null;
  instructions: string;
  wake_policy: { important_event_types: string[] };
  enabled_tools: string[];
  default_wake_interval_minutes: number;
  min_wake_interval_minutes: number;
  max_wake_interval_minutes: number;
  terminal_order_statuses: string[];
  order_status_by_event: Record<string, string>;
}

/** RunCreate: POST /api/runs. */
export interface RunCreateBody {
  order_id: string;
  supervisor_id: string;
  run_instructions: string[];
}

/** EventCreate: POST /api/runs/{run_id}/events. `payload` is a free-form JSON object. */
export interface EventCreateBody {
  event_type: string;
  payload: Record<string, unknown>;
}

/** Accepted: the 202 body. It means "delivered to the workflow boundary", NOT "processed". */
export interface Accepted {
  run_id: string;
  request: string;
  accepted: boolean;
}

/** InstructionCreate: POST /api/runs/{run_id}/instructions. The backend rejects a blank text and strips it. */
export interface InstructionCreateBody {
  instruction: string;
}

/**
 * The part of GET /api/runs/{run_id}/status (the workflow's get_status Query) the Human Controls use.
 * `state` is the workflow's own: "reasoning" | "sleeping" | "paused" | "terminal". Pause is NOT visible in
 * `runs.status` (a paused run still reads "running"), so this is the only place a pause shows.
 */
export interface WorkflowStatus {
  state: string;
  interrupt_count: number;
  reasoning_count: number;
  last_cycle_outcome: string | null;
}
