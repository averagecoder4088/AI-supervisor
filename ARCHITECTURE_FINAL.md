# Final Architecture & Workflow

## 1. Document Status

- **What this is.** The final architecture description of the implemented Order Supervisor POC, as of the repository at the commit that added this file.
- **Source of truth.** The code under `backend/app/` and `frontend/src/`, the tests, and the scripts. Where this document and the code disagree, the code wins. It supersedes the original specification (`DOCS/Order_Supervisor_Final_Architecture_Specification (1).docx`) where they conflict; section 16 lists the differences.
- **Scope.** A proof of concept, not a production system (sections 17 and 18).
- **Validation in one line.** 313 backend tests, a real Temporal runtime run, a live Gemini smoke test and a real Gemini + Temporal end-to-end run (section 14).
- **Diagrams.** The four Mermaid diagrams live in the root [`README.md`](README.md) and are not repeated here.

## 2. Executive Architecture Summary

| Point | Final architecture (verified in the code) |
|---|---|
| Unit of supervision | **One long-running Temporal `OrderWorkflow` per order.** Workflow id `order-{order_id}` (`order_workflow_id()` in `backend/app/temporal/constants.py`), task queue `order-supervisor`. |
| Technology | **Fixed by the assignment:** Next.js (App Router), Tailwind CSS, FastAPI, Temporal Python SDK, PostgreSQL. **Chosen:** async SQLAlchemy 2.0 with Alembic migrations, plain LLM SDK calls returning structured JSON (no agent framework), a rule-based wake policy. |
| Orchestration | **Temporal** owns the workflow lifecycle, Signals, durable timers and compact workflow state. |
| API boundary | **FastAPI** validates requests, reads and writes application rows, and starts, signals, queries and terminates the workflow. |
| Frontend | **Next.js App Router**: Server Components read, Server Actions write, a small client component refreshes the page while a run is active. The browser never contacts FastAPI, Temporal or PostgreSQL. |
| Persistence | **PostgreSQL** holds application and history data (11 tables: 8 application tables and 3 mock operational tables). It is not the workflow engine. |
| Non-deterministic work | **Activities** perform every LLM call, database write and tool execution. |
| LLM | Used for **structured reasoning only**. It returns a JSON decision and has no side effects; the workflow validates the decision and runs any tool through an Activity. |
| Determinism | Workflow code does no I/O and uses only `workflow.now()`, Temporal timers and `workflow.uuid4()`. |
| Wake-ups | A **wake reason** decides when reasoning happens: workflow start, an important event Signal, or a durable-timer expiry (the assignment's three triggers), plus a run instruction and Resume. |
| Lifetime | The workflow stays alive until the order reaches a **configured terminal order status** (completion) or is **hard-terminated** by a human. |

Two points are easy to get wrong and are checked against the code throughout: **terminal is not an LLM decision** (section 4), and **Signal-With-Start is not used by the application** (section 4, "Workflow startup").

## 3. Final System Architecture

### Frontend (`frontend/`)

- Next.js 16 (App Router), TypeScript, Tailwind CSS. Server Components read from FastAPI; Server Actions write to it; the browser never contacts the backend.
- Pages: `/`, `/supervisors`, `/supervisors/new`, `/runs/new`, `/runs/[runId]`.
- Details in section 13.

### API (`backend/app/api/`)

FastAPI is a thin boundary over PostgreSQL and the Temporal client. 18 endpoints:

| Group | Endpoints |
|---|---|
| Health | `GET /health` |
| Supervisors | `POST /api/supervisors`, `GET /api/supervisors/{id}` |
| Runs | `POST /api/runs`, `GET /api/runs`, `GET /api/runs/{run_id}` |
| Events and instructions | `POST /api/runs/{run_id}/events`, `POST /api/runs/{run_id}/instructions` (both `202`) |
| Human controls | `POST /api/runs/{run_id}/pause`, `/resume`, `/interrupt`, `/terminate` (all `202`) |
| Observation | `GET /api/runs/{run_id}/status` (Temporal Query), `/timeline`, `/memory`, `/actions`, `/tool-executions`, `/final-output` (PostgreSQL) |

Responsibilities and boundaries:

- **Start once, then signal.** `POST /api/runs` saves the run as `starting`, calls `start_workflow`, then records `running` (or `failed`). Every later request only signals, queries or terminates an existing workflow.
- **`202` means accepted at the workflow boundary**, not processed. The workflow records the event a moment later.
- **The API never writes events, timeline entries, actions, memory or final output.** Those are written by the workflow's Activities. The API writes supervisors, runs and run status.
- **Errors** use one shape, `{"error": <message>, "code": <CODE>}`: `VALIDATION_ERROR` (400), `RUN_NOT_FOUND` (404), `RUN_ALREADY_EXISTS`, `RUN_NOT_ACTIVE`, `WORKFLOW_ALREADY_STARTED` (409), `TEMPORAL_UNAVAILABLE` (503), and a few more.
- **Degraded mode.** The API creates one Temporal client in its lifespan. If Temporal is unreachable at startup it logs a warning and still serves database-only endpoints; Temporal-dependent endpoints answer `503 TEMPORAL_UNAVAILABLE`. It does not reconnect later, so it must be restarted if it was started before Temporal.
- **Observation** is read-only: history from PostgreSQL (oldest first, no pagination), live state only from Temporal's `get_status` Query, with `QueryRejectCondition.NOT_OPEN` so a closed workflow answers `409 RUN_NOT_ACTIVE` instead of being replayed into stale state. The status Query has a 5 second RPC timeout.

### Temporal

- **One workflow per order**, id `order-{order_id}`. A second workflow with the same id cannot be started while one is running (`WorkflowAlreadyStartedError` becomes `409 WORKFLOW_ALREADY_STARTED`); PostgreSQL additionally enforces one run per `order_id`.
- **Signals** change a running workflow: `submit_event`, `add_run_instruction`, `pause`, `resume`, `interrupt`. Handlers are synchronous and side-effect free: they update in-memory state and queue records that the main loop persists through Activities.
- **Query:** `get_status` returns the compact operational state (`OrderWorkflowStatus`).
- **Timers:** the scheduled wake-up is a real Temporal timer (`workflow.wait_condition(..., timeout=remaining)`).
- **Activities** do all I/O (section 3, Activities).
- **Termination** is Temporal's client-side `handle.terminate(...)`, not a Signal.
- **Signal-With-Start:** not used by the application; see section 4.
- **Task queue and worker:** one task queue, `order-supervisor`. The worker (`python -m app.temporal.worker`) hosts the workflow and all Activities.

### OrderWorkflow (`backend/app/temporal/workflows.py`)

- **Initialization:** state is set up in `@workflow.init`, before any Signal handler can run, from `OrderWorkflowInput` (a snapshot of the supervisor configuration plus the run's instructions).
- **State (compact only):** order status, workflow state (`reasoning`, `sleeping`, `paused`, `terminal`), the scheduled wake time, the wake reason, reasoning and interrupt counts, `pending_events` (recorded but not yet seen by a cycle), a bounded window of the 20 most recent events, memory, the last decision and cycle outcome, run instructions, a bounded log of the last 20 tool outcomes. Full history lives in PostgreSQL.
- **Main loop:** persist queued records; if the order status is terminal, leave the loop; if paused, wait; if a wake reason is set, run one reasoning cycle; otherwise sleep on the timer. It never spins.
- **Wake reasons** (`WakeReason`): `workflow_start`, `important_event`, `scheduled_wakeup`, `resume`, `instruction_added`.
- **Terminal handling:** see section 4.

### Activities (`backend/app/temporal/activities/`)

| Activity | Responsibility |
|---|---|
| `record_event` | Insert the event and its timeline entry, and update `runs.order_status` when the event maps to a status. |
| `record_timeline_entries` | Insert a batch of queued timeline entries. |
| `record_action_started` / `record_action_finished` | Create the action and tool-execution rows (`pending`), then complete them with the outcome and a timeline entry. |
| `save_memory_snapshot` | Insert a memory snapshot. |
| `save_run_instructions` | Write `runs.run_instructions`. |
| `complete_run` | Insert the final output (one row per run) and set `runs.status = completed`. |
| `generate_reasoning_decision` | Build the prompt, call the LLM client, validate the JSON, return a typed decision. |
| `generate_final_output` | Same for the final report. |
| `execute_tool` | Dispatch one of the four tools. |

Retry categories are in section 11.

### PostgreSQL

Stores: `supervisors`, `runs`, `events`, `timeline_entries`, `actions`, `tool_executions`, `memory_snapshots`, `final_outputs`, plus three mock operational tables (`mock_orders`, `mock_shipments`, `mock_customer_messages`). It deliberately does **not** run workflows, schedule wake-ups or hold the live workflow state. The application never polls it to simulate supervision. Three Alembic migrations create the schema (head `821bc1bc7abf`).

- **Relationships:** `supervisors` 1:N `runs`; `runs` 1:N `events`, `timeline_entries`, `actions` and `memory_snapshots`; `actions` 1:N `tool_executions`; `runs` 1:1 `final_outputs` (unique on `run_id`). `runs.order_id` is unique, and a supervisor is unique on `(name, version)`.
- **Action vs tool execution:** an *action* is what the AI decided to do; a *tool execution* is what actually happened when that decision was attempted. Likewise an *event* is a raw incoming record, while a *timeline entry* is the broader human-readable history.

### LLM (`backend/app/llm/`)

- **Abstraction:** the `LLMClient` protocol has one method, `generate_json(system_prompt, user_prompt, schema_name, json_schema) -> str` (the raw JSON text). The Activities validate the text; the client only transports.
- **`FakeLLMClient`** (`fake.py`): a deterministic scripted double. It returns queued responses in order (a string, an exception to raise, or an async callable) and falls back to a valid default decision or final output when its script is empty. It records every call. It is injected only by the tests and by `runtime_validation.py`; it is **not selectable through configuration**.
- **`OpenAILLMClient`** (`client.py`) with two providers behind the same protocol:
  - `openai` (the code default): the OpenAI Responses API with strict `json_schema` output and `store=False`.
  - `gemini`: Google's OpenAI-compatible endpoint through the same `openai` SDK, using **Chat Completions** with a strict `json_schema` `response_format` and, only when configured, `reasoning_effort` (default model `gemini-3.1-flash-lite`, no reasoning effort sent). It uses Chat Completions because that endpoint does not serve the Responses API.
- **Retries belong to Temporal:** the SDK's own retries are off (`max_retries=0`) and the client timeout is 45 seconds, below the Activity timeouts.
- **Provider boundary:** provider errors are translated into three classes (non-retryable authentication, non-retryable not-configured, retryable provider error); key material is redacted and never reaches logs, the timeline or the database.

### Tools (`backend/app/tools/`)

Four fixed tools, described in section 10. They act on the mock operational tables in PostgreSQL; nothing external is contacted.

## 4. Final Order Workflow Lifecycle

### Workflow startup

The relationship between starting a workflow, Signals and initialization, verified against the code:

| Question | Answer |
|---|---|
| Where is a workflow started? | Exactly one place: `POST /api/runs` (`create_run` in `backend/app/api/runs.py`) calls `client.start_workflow(OrderWorkflow.run, input, id=order-{order_id}, task_queue=order-supervisor)`. There is no other `start_workflow` call in the application. |
| Is Signal-With-Start used? | **No.** The application never uses Temporal's Signal-With-Start. It appears in one place only: a regression test (`test_signal_delivered_at_workflow_startup_sees_initialized_state` in `backend/tests/test_order_workflow.py`), which starts a workflow with `start_signal` on purpose. |
| Case 1: the workflow does not exist | Nothing starts it. An event, instruction or control for a run whose workflow is missing or closed is refused with `409 RUN_NOT_ACTIVE` (the API checks the run's status first, and a Temporal `NOT_FOUND` also maps to 409). **No incoming event can start a workflow.** |
| Case 2: the workflow already exists | The API sends an ordinary Signal to the existing workflow (`get_workflow_handle(...).signal(...)`). |
| Why then does the test use Signal-With-Start? | To prove a property of the workflow, not of the API: in the Temporal Python SDK a Signal handler task is queued before `run()`, so a Signal delivered in the very first activation must already see the configuration. That is why state is initialized in `@workflow.init` instead of `run()`. The test delivers an important event together with the start and asserts that the event is recorded, the status mapping is applied, and the event is consumed by the workflow-start reasoning pass (`reasoning_count == 1`, last wake reason `workflow_start`, no second cycle). |
| Is there a window where the workflow exists but the run row is still `starting`? | Yes. The API saves `starting`, starts the workflow, then records `running`. `starting` counts as active, so a Signal in that window is accepted, and `@workflow.init` makes it safe. |

If the workflow starts but recording `running` fails, the API answers `500 RUN_STATE_UPDATE_FAILED` and leaves the run `starting`; it does not terminate a correctly running workflow. If starting the workflow fails, the run is marked `failed` and keeps its `order_id`.

### End-to-end flow

1. The UI, a script or the simulator submits an order event through FastAPI.
2. FastAPI validates it and sends a `submit_event` Signal to the order's workflow.
3. The Signal handler queues the event and updates state without doing heavy work.
4. The wake policy decides whether the supervisor reasons now; the event is recorded either way.
5. If it reasons, the workflow runs the reasoning Activity and validates the returned decision.
6. It runs at most one tool through an Activity and records the action and its outcome.
7. It updates the compact memory and schedules the next wake, then sleeps on a durable timer.
8. When the order reaches a terminal status it generates and stores the final output, marks the run completed and ends.

### Wake triggers

| Trigger | Wake reason | Status |
|---|---|---|
| Workflow start | `workflow_start` | **Primary (assignment)** |
| Important incoming event | `important_event` | **Primary (assignment)** |
| Scheduled wake-up (timer expiry) | `scheduled_wakeup` | **Primary (assignment)** |
| Run instruction added | `instruction_added` | Additional |
| Resume after Pause | `resume` | Additional |

`run()` sets `workflow_start` before entering the loop. A Signal sets `important_event` or `instruction_added` only if the workflow is not paused, the order is not terminal, and no wake is already queued, so wakes do not stack. Resume sets `resume`. The timer sets `scheduled_wakeup` when it expires. Every event is recorded whatever the wake policy answers.

### Reasoning cycle

```
wake reason set
  -> reasoning Activity (LLM returns structured JSON, validated in the Activity)
  -> workflow checkpoint: discard if interrupted, paused, or the order became terminal
  -> workflow validation: tool exists, enabled for this supervisor, one tool at most, required inputs present; wake interval clamped
  -> optional tool (record action started -> execute_tool Activity -> record action finished)
  -> memory update (capped) and memory snapshot
  -> schedule the next wake
  -> wait
```

- **Stale or discarded decisions.** If the cycle was interrupted, the supervisor was paused, or the order became terminal while the LLM was thinking, the decision is **discarded**: no tool runs, no memory snapshot is saved, and the pending events stay pending. The workflow records only the cycle outcome (`interrupted`, `discarded_paused` or `discarded_terminal`) and a system timeline entry, then schedules its next wake (or stays paused; for a terminal order it goes to terminal handling). A failed LLM call (after retries) ends the cycle the same way with outcome `llm_failed`.
- **A rejected tool request is different.** If the tool fails validation (unknown, not enabled, more than one, missing inputs) the tool is dropped and noted on the timeline, but the decision's memory update and next wake still apply.
- **`reasoning_count`** counts only completed cycles; failed, interrupted and discarded attempts appear in `last_cycle_outcome`.

### Sleep and wake

The workflow sleeps on a **durable Temporal timer** until the scheduled wake time, or until a Signal sets a wake reason, the run is paused, the order becomes terminal, or queued records need persisting. The next wake time is the LLM's requested interval clamped to the supervisor's minimum and maximum, or the default. A routine event does not move the scheduled time. There is no continuous LLM loop: the LLM runs only when a wake reason is set, and the workflow otherwise holds no thread and no process state (Temporal replays it if the worker restarts).

### Terminal state

**Terminal status is not an LLM decision.** A run ends when the order's status reaches one of the supervisor's configured *terminal order statuses*. The status changes only through the workflow's event logic: when an event arrives whose type the supervisor maps to a status (`order_status_by_event`), the workflow sets it. There is no built-in mapping. The main loop checks the terminal condition before pausing or reasoning, so no further reasoning cycle runs. Then, in `_handle_terminal_order_status`:

1. mark the state `terminal`, clear the wake, queue a system timeline entry, flush records;
2. run the **final-output Activity**; if it fails after retries, build a deterministic **fallback** from the recorded state;
3. **flush again** (R1, below), then run `complete_run`, which writes the final output and sets `runs.status = completed`;
4. **drain** any records still queued while `complete_run` ran, then the workflow ends.

A terminal order completes even while the supervisor is paused (pause stops reasoning, not completion). A decision in flight when the order becomes terminal is discarded (`discarded_terminal`).

**R1, the terminal flush and drain.** While the final output is generated (seconds to minutes with a real LLM) the run is still open, so the API keeps accepting events, instructions and controls. Before R1 those Signals were never persisted because the workflow ended. R1 added a flush after final-output generation and a drain loop after `complete_run`. Late events are now recorded, but they do not wake reasoning, reopen the run or change the final output.

### Hard termination

`POST /api/runs/{id}/terminate` calls Temporal's client-side `terminate`, then the API records `runs.status = terminated`.

| | Pause | Interrupt | Terminate |
|---|---|---|---|
| Workflow | stays alive | stays alive | **closed** |
| Mechanism | Signal | Signal | Temporal client call, not a Signal |
| Effect | stops reasoning until Resume | drops the current cycle | stops everything; workflow code cannot observe it |
| Final output | generated if the order later reaches a terminal status | same | **not generated** |

Terminate leaves `completed_at` empty and offers no compensation for work already done. A terminated run answers `409 RUN_NOT_ACTIVE` to any later control.

## 5. Final Event Model

- **Ten event types** (fixed, validated vocabulary): `order_created`, `payment_confirmed`, `payment_failed`, `shipment_created`, `shipment_delayed`, `delivered`, `refund_requested`, `customer_message_received`, `no_update_for_n_hours`, `order_cancelled`. An event has a type and a free-form JSON object payload.
- **Delivery.** An event reaches the workflow as a `submit_event` Signal (through `POST /api/runs/{id}/events`).
- **Three separate things happen, and they must not be confused:**

| Concern | What happens |
|---|---|
| **Event persistence** | Every event is recorded in `events` with a timeline entry (`record_event`), whether or not it wakes the LLM. |
| **Event-triggered reasoning** | The wake policy decides. It is rule-based and deterministic: an event wakes the supervisor only if its type is in the supervisor's `important_event_types` and the workflow is not paused, the order is not terminal, and no wake is already queued. |
| **Workflow state change** | The event is added to `pending_events` and counted; if the supervisor maps the event type to an order status, the order status changes (and `runs.order_status` is updated when the event is persisted). |

- **Events recorded without waking:** all events that are not in the important list (for the default list `shipment_delayed`, `payment_failed`, `refund_requested`, `order_cancelled`; any subset of the ten can be chosen per supervisor), and every event while paused. They stay in `pending_events` and are seen by the next cycle, whatever wakes it.
- **Terminal precedence.** `order_cancelled` can be both important and terminal; terminal wins, so the run goes straight to final output.

## 6. Human Control Model

### Pause
The workflow stays alive and stops supervisor reasoning. It clears any queued wake and the scheduled wake time. Incoming events and instructions are still recorded but wake nothing. Pause is honored at the next checkpoint: a cycle already in flight finishes its LLM call, and its decision is discarded (`discarded_paused`); a tool already started is not cancelled. Pause does not change `runs.status`, which stays `running`; the paused state is visible only in the workflow's `state` (`paused`) through the status Query.

### Resume
Releases the pause and sets the wake reason `resume`, so the supervisor re-evaluates everything recorded meanwhile (the pending events).

### Interrupt
Stops the current reasoning cycle and keeps the workflow alive.
- LLM call in flight: the Activity is **abandoned** and its decision discarded (`interrupted`).
- Decision already returned but not yet applied: discarded at the checkpoint.
- **Tool already started: it is not cancelled.** It finishes, its outcome is persisted, and the cycle completes normally; completed work is never undone.
- Sleeping: a queued but unstarted wake is cleared.

### Terminate
Hard Temporal termination (section 4). Not a Signal; the workflow does not continue and no final output is written.

## 7. Memory vs Timeline vs Database

| | What it is | Where it lives | Purpose |
|---|---|---|---|
| **Timeline** | Chronological, human-readable record of what happened (`event`, `action`, `control`, `instruction`, `decision`, `system` entries) | PostgreSQL `timeline_entries` | Observation and audit. Never sent whole to the LLM. |
| **Memory** | Compact, evolving reasoning context: `situation_summary` (at most 1000 characters), up to 5 `open_concerns` (200 characters each), `last_action`, `last_wake_reason`, `cycle_count` | The workflow's state (the working copy) and PostgreSQL `memory_snapshots` (one snapshot per applied cycle, for observability) | What the LLM is given each cycle, rewritten each cycle. Not the source of truth. |
| **PostgreSQL** | Durable application persistence | The 11 tables | Queryable history for the UI and evidence for the summaries. |
| **Temporal workflow state** | Durable orchestration state | Temporal's workflow history | Continuing the workflow after restarts: pending events, a bounded recent-event window, memory, next wake, control flags. |

They are separate on purpose. Memory is lossy by design (capped and rewritten), so it cannot be the history; the timeline is complete but too large to send to the LLM every turn; Temporal's history is for orchestration and replay, not for queries. The LLM input is the compact memory plus new and recent events, never the full timeline.

## 8. Supervisor Configuration and Run Configuration

**Supervisor** (a reusable template, table `supervisors`): `name`, optional `description`, `instructions` (the base instruction for every run), `enabled_tools`, `wake_policy` (`{"important_event_types": [...]}`), `default_wake_interval_minutes`, `min_wake_interval_minutes`, `max_wake_interval_minutes` (validated as `0 < min <= default <= max`), `terminal_order_statuses`, `order_status_by_event` (optional event to status mapping), and `version`.

**Versioning.** Supervisor rows are immutable: there is no update endpoint. Creating a supervisor with a new name gives version 1; with an existing name it inserts the next version (`max + 1`); a concurrent duplicate `(name, version)` is refused with `409 SUPERVISOR_VERSION_CONFLICT`. A run points at one specific supervisor row, so its configuration cannot change under it. The run's `supervisor_version` is read from the linked row, not stored again. One shared validator (`validate_supervisor_config`) applies the same rules at creation and when a workflow starts.

**Run** (table `runs`): `order_id` (unique), the supervisor link, `status` (`starting`, `running`, `completed`, `terminated`, `failed`; the legacy default `active` is tolerated as active), `order_status`, `run_instructions` (JSON list of `{text, added_at}`), and timestamps. At start the API builds `OrderWorkflowInput` from the supervisor row and the run, a snapshot the workflow keeps for its whole life.

**Why the two behave differently.** The supervisor configuration is a template shared by many runs, so it is frozen per version. Run-specific instructions are the operator's live steering of one order: they are added through a Signal, stored by the workflow (`save_run_instructions`), included in every later reasoning input, and wake the supervisor if it is not paused.

## 9. LLM Decision Contract

The reasoning decision (strict JSON schema, no extra properties, every field required, missing values are `null`) has exactly these fields:

| Field | Meaning |
|---|---|
| `assessment` | Non-empty short reasoning; recorded on the timeline and on the action. |
| `tool` | One of the four tool names, or `null`. A list is invalid. |
| `tool_input` | `reason`, `priority` (`low`, `medium` or `high`), `message`; each may be `null`. The chosen tool's required inputs must be present. |
| `next_wake_in_minutes` | Requested wake interval, or `null` for the default. |
| `memory_update` | `situation_summary` and `open_concerns`. |

The final output has `summary`, `key_actions`, `key_learnings` and `recommendations` (the workflow adds `source`).

- **The LLM decides** what it makes of the situation, at most one tool, when to look again, and how to rewrite its memory.
- **The workflow validates** the decision after the Activity has validated the JSON against the strict schema (an invalid answer is retried, so the model is asked again). It discards a stale decision, checks the tool against the fixed registry and the supervisor's enabled tools, allows one tool with its required inputs, clamps the wake interval to the supervisor's bounds, and caps memory.
- **The LLM cannot** run a tool, write to any database, change workflow state or set the order status. It only returns text that the deterministic workflow interprets.
- **Why structured output:** the workflow must act on the answer without parsing prose, and the strict schema plus local validation make the contract enforceable rather than hoped for.
- **Invalid, stale, rejected:** invalid JSON is retried (up to 3 attempts); an LLM failure after retries ends the cycle as `llm_failed` (no tool, events stay pending); a stale decision is discarded; a rejected tool is dropped while the rest applies.

## 10. Tool Execution Model

The tool set is fixed. The LLM never chooses the order: the tool always acts on the run's own order id.

| Tool | Purpose | Kind | Mock-world behavior | Retry |
|---|---|---|---|---|
| `get_order_status` | Read the order's status | Read-only | Reads `mock_orders`; `Order not found` if no row | Retried (3 attempts) |
| `get_shipment_status` | Read the shipment | Read-only | Reads `mock_shipments`; fails with `Order not found` or `Shipment not found` | Retried (3 attempts) |
| `escalate_shipment` | Escalate a shipment problem (needs `reason`, `priority`) | Side effect | Sets `mock_shipments.escalated`; idempotent by state (already escalated means no write) | **One attempt** |
| `send_customer_update` | Message the customer (needs `message`) | Side effect | Inserts one outbound row in `mock_customer_messages` per call | **One attempt** |

- **Business failures** (unknown order, no shipment, bad input) return `success=False` and are recorded as failed tool executions; they are not retried. Unexpected infrastructure errors raise and follow the retry rule of the tool's class.
- **The mock world.** The mock tables are created by the simulator and the test factories. **Starting a run from the UI does not create them, and events injected in the UI do not change them**, so for an unseeded order every tool call is recorded as a failed execution. This is a documented POC boundary; the README explains how to seed an order.
- **Why the workflow does not execute side effects itself:** workflow code must be deterministic and replayable. A side effect inside it would repeat on replay. As an Activity it runs once per attempt with an explicit timeout and retry policy, and its result is recorded in Temporal's history.

## 11. Reliability and Retry Model

| Operation | Timeout | Retry policy | Why |
|---|---|---|---|
| Persistence Activities | 10 s | 5 attempts, backoff 1 s up to 10 s | Idempotent, so retrying is safe. |
| Reasoning (LLM) | 60 s | 3 attempts, first delay 1 s | Transient provider failures and invalid output are worth another try. |
| Final output (LLM) | 90 s | 3 attempts | Same; larger budget for a longer answer. |
| Read tools | 10 s | 3 attempts | No side effect, so a retry is harmless. |
| Side-effect tools | 10 s | **exactly 1 attempt** | A retry could duplicate an escalation or a customer message. |
| LLM client (inside the Activity) | 45 s, SDK retries off | none | Temporal owns retries; the client fails before the Activity timeout. |

- **LLM error classes:** authentication and not-configured errors are non-retryable; provider errors (timeout, rate limit, 5xx) and invalid output are retryable.
- **Idempotency.** Row ids are generated in the workflow with `workflow.uuid4()` and passed into the Activities; inserts use `ON CONFLICT DO NOTHING`; updates write absolute values; `final_outputs` is unique per run. A retried persistence Activity therefore cannot create a duplicate. `escalate_shipment` is idempotent by state.
- **Known limitation of `send_customer_update`.** It does not use the workflow's idempotency key: each call inserts a new message. Protection against duplicates rests entirely on the single-attempt rule. The residual risk is the opposite one: if the Activity times out after the message was committed, the message exists but the tool execution is recorded as failed.
- **Persistence failure.** If a persistence Activity fails all 5 attempts (for example a database outage of about 30 seconds or more) the error propagates and the workflow **fails explicitly**; unlimited retries were deliberately not adopted.
- **LLM failure.** A failed reasoning cycle is recorded and the workflow continues (`llm_failed`). A failed final-output generation never undoes the terminal state: the deterministic fallback is stored (`source: "fallback"`).
- **PostgreSQL and Temporal are not one transaction.** Run creation reports each step's outcome honestly (section 4).

The system is not production-hardened; see sections 17 and 18.

## 12. Final Output Model

- **Detection:** the order status reaches a configured terminal status (section 4). No LLM decision is involved.
- **Generation:** the final-output Activity receives the final order status, compact memory, recent events, the bounded action log, event and cycle counts, and the instructions, and returns `summary`, `key_actions`, `key_learnings`, `recommendations`.
- **`source`:** `llm` when the model produced the output, `fallback` when the workflow built a deterministic one from the recorded state after generation failed.
- **Persistence and completion:** `complete_run` writes one `final_outputs` row and sets `runs.status = completed`; then the workflow ends. Until a run completes, `GET /final-output` returns `final_output: null`.
- **Not produced** for a terminated run.

## 13. Frontend Architecture

- **Reads and writes.** Server Components fetch server-side; Server Actions perform every write. The browser never calls the backend.
- **Why Server Actions:** the backend has no CORS configuration, and server-to-server calls need none.
- **Run page** (top to bottom): overview (the header shows both the run status and, when readable, the live workflow state), workflow status, human controls, instructions (the supervisor's read-only base instruction, then the run's additional instructions and a form), event injection, then memory, timeline (oldest first), actions, tool executions, final output. Creation flows: supervisor form (tools, wake behavior, terminal statuses, status mapping) and run form (order id, supervisor, run instructions). The dashboard splits active from completed and ended runs.
- **Human controls:** which buttons appear depends on the workflow's own state from the status Query (Pause, Interrupt, Terminate while running; Resume, Interrupt, Terminate while paused), because a paused run still reads `running`. Terminate needs an explicit confirmation, enforced again in the Server Action. If the state cannot be read the buttons are shown disabled ("Unable to determine the workflow state").
- **Live polling.** `LiveRefresh` calls `router.refresh()` every 3 seconds, so the same Server Components re-read every source; it also owns the manual Refresh button so the two share one in-flight flag. It polls while `runs.status` is active (a paused workflow is still active), skips hidden tabs, and stops when the run is no longer active or the workflow query has reported the workflow closed on two consecutive renders. A failing status query or an unreachable backend does not stop it. The `/status` read is capped at 3 seconds so a dead Temporal cannot make each refresh slow.
- **Why polling:** it reuses the server-rendered pages with no new endpoint or dependency; WebSocket and SSE were out of scope.
- **Failure isolation.** Each observation source loads independently; one failing or malformed source shows "unavailable" in its own section without breaking the page.
- **Stale and closed workflows.** A control sent to a run that ended answers `409` and the UI says the run state changed; a closed workflow shows no controls.
- **Testing.** No automated frontend tests; checked with lint, typecheck, build and manual browser runs.

## 14. Runtime / Validation Architecture

### Offline
- **313 backend tests**, deterministic, against a real local PostgreSQL and Temporal's time-skipping test server, with `FakeLLMClient` as the LLM.
- **Five end-to-end scenarios** (S1 smooth delivery, S2 delayed shipment, S3 LLM unavailable, S4 human controls, S5 payment failure and cancellation) drive the real API, workflow, Activities and mock world with a scripted LLM. The scheduled wake is crossed by skipping time.

### Real Temporal (`backend/scripts/runtime_validation.py`)
- A real Temporal dev server, a real worker (the production `create_worker` with `FakeLLMClient`), a real FastAPI process and real PostgreSQL, as separate processes.
- It runs S1 with a 1-minute wake interval so a real durable timer fires, proven from Temporal history.

### Real LLM
- `llm_smoke.py`: 2 real Gemini requests (a decision and a final output) through the runtime's own client, validated with the same local checks as the workflow.
- `gemini_e2e.py --mock-llm`: the full pipeline against a local OpenAI-compatible stub. It proves the pipeline and the HTTP request shape, not Gemini itself.
- `gemini_e2e.py` (real mode): the same pipeline against the real Gemini API. Real decisions on start, on an important event and on a durable timer; real tool Activities changed the mock world; the final output was written by Gemini (`source: llm`).
- Gemini can return transient 503 or 429 errors, which fail a cycle cleanly and are retried at the next wake.

| Statement | Status |
|---|---|
| **Real Gemini smoke test** | **VERIFIED** |
| **Real Gemini + Temporal end-to-end** | **VERIFIED** (2026-09-25, `gemini_e2e.py`, real Gemini `gemini-3.1-flash-lite`; the first attempt hit transient Gemini 503 responses and failed the script's strict clean-run check, a second attempt passed; a further real run seeded with the README SQL and given a live instruction also passed) |
| Real Temporal runtime with FakeLLM | Verified |
| Full pipeline against a local stub (`--mock-llm`) | Verified (not Gemini) |
| Real OpenAI provider | **Not validated live**; the adapter is covered by offline tests only |
| Frontend flows | Exercised in a real browser against the real stack with FakeLLM; not with a real LLM |

## 15. Important Architectural Decisions

| Decision | Final choice | Why | Alternative considered |
|---|---|---|---|
| Orchestration vs database | Temporal runs the workflow; PostgreSQL is application and history data | Fixed by the specification: Temporal is the durable engine, not the reporting database | Cron or a loop polling PostgreSQL, or Celery/RQ tasks that sleep (ruled out: no durability, no replay, hand-built timers and retries; polling is also forbidden by the specification's rules) |
| Workflow granularity | One `OrderWorkflow` per order, id `order-{order_id}` | The order is the natural unit of state; a core requirement | A workflow per event, per cycle, or global (rejected) |
| Events into the workflow | Signals (`submit_event`) | Something happened; the workflow decides whether to wake | Polling for events (ruled out) |
| Signal-With-Start | **Not used by the application.** The API starts once (`start_workflow`) and afterwards only signals existing workflows; `@workflow.init` makes a startup-time Signal safe, proven by one test | Keeps "start" and "deliver" separate; an event cannot create a run | No decision to adopt Signal-With-Start in the API is recorded; it was not formally weighed |
| Where state is initialized | `@workflow.init`, not `run()` | A Signal in the first activation must see the configuration | Initializing in `run()` (rejected) |
| Timer-based wake-ups | Durable Temporal timer (`wait_condition` with a timeout) | Sleep without a process; wake on schedule | No alternative recorded beyond the specification |
| I/O placement | Activities for LLM, database and tools | Workflow code must stay deterministic | Side effects in workflow code (forbidden by the specification) |
| LLM output | Strict structured JSON validated in the Activity and again in the workflow | The workflow acts on data, not prose | None recorded |
| FakeLLM | Deterministic scripted double for tests and validation | Repeatable tests without network or key | Real provider in tests (rejected: the suite stays offline) |
| Real provider | Gemini through the OpenAI-compatible endpoint, Chat Completions | A key-less probe showed the endpoint answers the Responses API with 404 but serves Chat Completions; a branch inside the existing client was the smallest change | A new client class or provider factory (excluded by the constraints) |
| OpenAI provider | Responses API adapter behind the same protocol; default provider in code | Sealed earlier; nothing changes unless another provider is selected | None recorded |
| Application data | PostgreSQL tables for events, timeline, actions, tool executions, memory snapshots, final output | Queryable history for the UI | None recorded |
| Memory vs timeline | Compact memory plus a full timeline | Memory is context, the timeline is history | Memory as the history (ruled out) |
| Wake policy | Rule-based list of important event types per supervisor | Deterministic and cheap | An LLM classifier (rejected); the assignment allows a simple policy |
| Terminate | Temporal client `terminate()`, not a Signal | A hard stop the workflow cannot intercept | A terminate Signal (rejected) |
| Tool set | Four fixed mocked tools; one tool per cycle | Assignment requires at least four, mocks allowed | None recorded |
| Mock world | PostgreSQL mock tables prepared by the simulator or by hand; **no automatic seeding** from the UI | Developer decision: the mocked-world setup is acceptable for this POC | Auto-creating mock rows on run start (not adopted) |
| Agent frameworks | No LangGraph, no multi-agent orchestration, no retrieval | Explicitly out of scope in the specification and the rules | None; excluded by rule |
| Browser writes | Next.js Server Actions | Removes the CORS question with no backend change | A minimal proxy (allowed, not chosen); CORS middleware (excluded) |
| Live observation | Client refresh every 3 s of server-rendered pages | Freshness without new infrastructure | WebSocket or SSE (excluded by scope; no comparison recorded) |
| Long histories | No `continue_as_new` in the POC | Scope; listed as optional in the assignment | No decision recorded beyond scope |
| Persistence failure | Bounded retries; then fail the workflow explicitly | Failure is visible, not hidden | Unlimited retries (not adopted) |
| Terminal while paused / during reasoning | Final output proceeds; a stale decision is discarded | Pause stops reasoning, not completion; never execute a stale tool | Wait for resume; execute anyway (rejected) |
| Supervisor versioning | Immutable rows, next version on the same name, version read from the linked row | Runs keep their semantics | Updating rows in place (rejected) |

Only decisions recorded during the project are listed; where no alternative was formally weighed, the table says so.

## 16. Architecture Evolution

The original specification was the implementation baseline. The differences that matter for the code:

| Specification | Final design | Why |
|---|---|---|
| 10 API endpoints | 18: added `pause`, six observation endpoints (including the `status` Query) and health | Pause is a specified control; the read side follows what the UI shows |
| Terminal: final output, mark complete, end | Adds a flush after final-output generation and a drain after `complete_run` (R1) | Signals accepted during terminal handling were never persisted |
| No event-to-status mapping | `order_status_by_event` on the supervisor | Without it no run could reach a terminal status through events |
| Interrupt: "stop the current cycle" | Abandon an in-flight LLM Activity, let a started tool finish, never undo completed work | A started side effect cannot be undone safely |
| "Mocked" tools | Deterministic stubs first, then a PostgreSQL-backed mock world (not seeded by the UI) | Observable effects without real integrations |
| Direct SDK calls | OpenAI Responses adapter, then Gemini through the same client (Chat Completions) | Gemini's endpoint serves Chat Completions, not Responses |
| Live state through a Query | Same, with `QueryRejectCondition.NOT_OPEN` and a 5 s timeout | Without it a closed workflow is replayed and reports stale state |
| UI deferred | Full Next.js UI with Server Actions and 3 s polling | Designed last, on top of the frozen backend |

## 17. Non-Goals and Deliberate Scope Boundaries

These are deliberate POC boundaries, not accidental omissions.

- Real commerce, shipping or messaging integrations; the tools are mocks.
- Authentication, authorization and multi-tenancy.
- Production hardening: secrets management, deployment, scaling, monitoring.
- Advanced retrieval (RAG), multi-agent orchestration, LangGraph.
- `continue_as_new` for very long histories.
- A supervisor list endpoint and a supervisor edit or delete flow.
- CORS and direct browser-to-API access.
- WebSocket, SSE or push-based live updates.
- An LLM wake classifier, agent-written wake guidance and special handling of unknown event types.
- A frontend test framework and production-scale frontend infrastructure.

## 18. Known Limitations / Unverified Areas

Open limitations:
- **`send_customer_update`** ignores the idempotency key (single-attempt rule only; residual "sent but recorded failed" risk).
- **A persistence outage of about 30 seconds or more fails the workflow.**
- **No `continue_as_new`;** `pending_events` is not bounded.
- **`terminate`** leaves `completed_at` empty and does not reconcile the run row if Temporal reports the workflow already closed; a `failed` run keeps its `order_id`.
- **The API connects to Temporal only at startup** and must be restarted if Temporal was not yet running.
- **The Temporal dev server keeps data in memory:** restarting it loses running workflows while PostgreSQL still shows their runs as `running`.
- **The supervisor picker** only lists supervisors used by existing runs.
- **Live updates** re-render the whole page every 3 seconds; if the backend goes down the page shows an error panel and a half-typed form draft is lost.
- **The timeline** is oldest first with no filtering or pagination.
- **Python 3.9** is the project's interpreter, which pins the `openai` SDK at 2.48.0.



## 19. Final Architecture Summary

- **One workflow per order.** One durable Temporal `OrderWorkflow` per order, id `order-{order_id}`. FastAPI starts it once when a run is created and afterwards only signals, queries or terminates it. The browser never touches Temporal or the database.
- **A deterministic workflow.** It holds compact state, records every event, and decides *when* to reason from a wake reason: workflow start, an important event Signal, a durable timer expiry, an added instruction or a resume.
- **Reasoning.** An Activity asks the LLM for a strict JSON decision. The workflow validates it, discards it if the situation changed, and otherwise runs at most one tool through another Activity, updates the compact memory and sleeps until the next wake. The LLM never causes a side effect.
- **Data.** PostgreSQL holds the history the UI reads; Temporal holds only orchestration state.
- **Ending.** A run ends when the order reaches a configured terminal status, not because the LLM says so. The workflow then writes a final report (with a deterministic fallback) and completes. A human can also hard-terminate it.
- **Status.** A proof of concept: 313 tests pass, and the real Temporal runtime, a live Gemini call and a full real Gemini + Temporal run were each validated (section 14). The OpenAI provider was not validated live.
