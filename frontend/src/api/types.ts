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
