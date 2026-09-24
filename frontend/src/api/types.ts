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
 * GET /api/runs/{run_id}/status: the workflow's own live state (its get_status Query). `state` is
 * "reasoning" | "sleeping" | "paused" | "terminal". Pause is NOT visible in `runs.status` (a paused run still
 * reads "running"), so this is the only place a pause shows. Only the fields this frontend reads are listed.
 */
export interface WorkflowStatus {
  state: string;
  order_status: string | null;
  next_wake_at: string | null;
  last_wake_reason: string | null;
  reasoning_count: number;
  interrupt_count: number;
  events_received: number;
  /** Events recorded but not yet seen by a reasoning cycle (only the number is shown). */
  pending_events: unknown[];
  terminal_order_status_reached: boolean;
  /** completed | llm_failed | interrupted | discarded_paused | discarded_terminal | null (no cycle yet). */
  last_cycle_outcome: string | null;
  final_output_persisted: boolean;
}

// ---- Observation (backend/app/api/observation.py, schemas.py). Each list endpoint answers oldest first.

/** timeline: event | action | control | instruction | decision | system. Only a message, no payload. */
export interface TimelineEntry {
  id: string;
  entry_type: string;
  message: string;
  created_at: string;
}

/** The supervisor's compact working memory: situation_summary, open_concerns, last_action, last_wake_reason, cycle_count. */
export interface MemorySnapshot {
  id: string;
  memory: Record<string, unknown>;
  created_at: string;
}

/** One tool call by the supervisor. `action_type` is the tool name; status is pending | completed | failed. */
export interface RunAction {
  id: string;
  action_type: string;
  status: string;
  reasoning: string | null;
  created_at: string;
  completed_at: string | null;
}

/** The execution of that tool. status is pending | success | failed; `error` is only set on failure. */
export interface ToolExecution {
  id: string;
  action_id: string;
  tool_name: string;
  status: string;
  input: Record<string, unknown>;
  result: Record<string, unknown> | null;
  error: string | null;
  started_at: string;
  completed_at: string | null;
}

/** The stored final output; `final_output` is null until the run completes. */
export interface FinalOutputResponse {
  final_output: FinalOutput | null;
  created_at: string | null;
}

export interface FinalOutput {
  summary?: string;
  key_actions?: string[];
  key_learnings?: string[];
  recommendations?: string[];
  /** "llm" | "fallback". */
  source?: string;
}
