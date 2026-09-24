# Order Supervisor

A proof of concept for a long-running AI supervisor that oversees **one order** from creation to completion. Every order gets its own durable [Temporal](https://temporal.io) workflow. Order events reach the workflow as Temporal Signals, an LLM decides when to act, when to sleep and when to wake up again, tools carry out the actions, and a final report is written when the order ends.

**Stack:** Next.js (App Router) + Tailwind CSS · FastAPI · Temporal Python SDK · PostgreSQL · an LLM behind a small client interface (Gemini through its OpenAI-compatible endpoint, or OpenAI).

The assignment is in [`DOCS/PROBLEM_STATEMENT.md`](DOCS/PROBLEM_STATEMENT.md) and the governing architecture specification is [`DOCS/Order_Supervisor_Final_Architecture_Specification (1).docx`](DOCS/Order_Supervisor_Final_Architecture_Specification%20%281%29.docx). This README describes the system **as it is actually implemented**.

---

## Contents

1. [Architecture](#architecture)
2. [Key design decisions](#key-design-decisions)
3. [Project structure](#project-structure)
4. [Prerequisites](#prerequisites)
5. [Setup](#setup)
6. [Environment configuration](#environment-configuration)
7. [Running the application](#running-the-application)
8. [Using the UI](#using-the-ui)
9. [API overview](#api-overview)
10. [Testing and verification](#testing-and-verification)
11. [What is real vs mocked](#what-is-real-vs-mocked)
12. [Limitations](#limitations)
13. [Troubleshooting](#troubleshooting)
14. [Demo walkthrough](#demo-walkthrough)

---

## Architecture

The responsibilities are split on purpose:

| Part | Responsibility |
|---|---|
| **Temporal** | Durable orchestration: the workflow's lifecycle, its Signals, its timers and its compact workflow state. |
| **`OrderWorkflow`** | Decides *when* the supervisor reasons and *what is allowed to happen*. It stays **deterministic**: no I/O, no randomness, time only through Temporal. |
| **Activities** | All non-deterministic or external work: LLM calls, PostgreSQL writes and tool execution. |
| **PostgreSQL** | Application and history data: supervisors, runs, events, timeline, actions, tool executions, memory snapshots, final output, plus the mock operational tables the tools act on. |
| **LLM** | Structured reasoning: it returns a JSON decision. It has **no side effects**; the workflow validates the decision and runs any tool itself, through an Activity. |
| **Tools** | Four mocked operational tools that read and change mock order, shipment and message rows in PostgreSQL. Those rows are created by the simulator and the tests, or seeded by hand (see [Setup](#setup)); the API and the UI do not create them. |
| **FastAPI** | A thin boundary over PostgreSQL and the Temporal client. |
| **Next.js** | The UI. The browser only ever talks to Next.js; Server Components read from FastAPI and Server Actions write to it. |

### System architecture

The browser never talks to FastAPI, Temporal or PostgreSQL directly.

```mermaid
flowchart TB
    subgraph BR["Browser"]
        live["LiveRefresh (client code)<br/>asks for a re-render every 3 s while a run is active"]
        user(["User"])
    end

    subgraph FE["Next.js App Router (frontend, port 3000)"]
        pages["Server Components<br/>read pages: dashboard, run page"]
        actions["Server Actions<br/>every write: forms and human controls"]
    end

    subgraph BE["FastAPI (backend/app/api, port 8000)"]
        api["REST API<br/>supervisors, runs, events, instructions,<br/>human controls, observation"]
        tclient["Temporal client<br/>start, Signal, Query, terminate"]
    end

    subgraph TS["Temporal server"]
        thist["Workflow history and durable timers<br/>task queue: order-supervisor"]
    end

    subgraph WK["Worker process (python -m app.temporal.worker)"]
        wf["OrderWorkflow<br/>one workflow per order, id: order-{order_id}<br/>deterministic state, Signals, Query, durable timers"]
        subgraph ACT["Activities: all I/O and non-determinism"]
            llmact["LLM Activities<br/>reasoning decision, final output"]
            persist["Persistence Activities<br/>events, timeline, actions, memory,<br/>instructions, complete run"]
            toolact["execute_tool Activity<br/>4 mocked tools"]
        end
    end

    pg[("PostgreSQL<br/>supervisors, runs, events, timeline_entries,<br/>actions, tool_executions, memory_snapshots,<br/>final_outputs, mock_* operational tables")]
    llm["LLM provider<br/>Gemini or OpenAI<br/>(FakeLLM only in tests and validation)"]

    user -->|"page views, only to Next.js"| pages
    user -->|"form submits"| actions
    live -.->|"router.refresh"| pages
    pages -->|"GET, server-side"| api
    actions -->|"POST, server-side"| api
    api -->|"reads history, writes supervisors and runs"| pg
    api --> tclient
    tclient -->|"start workflow, Signals, Query, terminate"| thist
    thist <-->|"worker polls the task queue"| wf
    wf -->|"schedules"| llmact
    wf -->|"schedules"| persist
    wf -->|"schedules"| toolact
    llmact -->|"structured JSON only"| llm
    persist --> pg
    toolact -->|"reads and changes mock order state"| pg
```

### Order workflow lifecycle

One `OrderWorkflow` per order (workflow id `order-{order_id}`). The main loop never spins: each pass persists what Signals queued, then either runs one reasoning cycle, waits while paused, or sleeps on a durable Temporal timer.

```mermaid
flowchart TD
    create["POST /api/runs<br/>order id, supervisor, run instructions"] --> starting["runs row saved as 'starting' in PostgreSQL"]
    starting --> start["FastAPI starts OrderWorkflow  id: order-{order_id}<br/>input: supervisor snapshot + run instructions"]
    start --> running["FastAPI records runs.status = 'running'<br/>('failed' if the workflow could not be started)"]
    running --> init["Workflow initializes its state<br/>empty memory, wake reason = workflow_start"]
    init --> loop["Loop pass: persist queued events, timeline<br/>and instruction changes (Activities)"]

    loop --> term{"Order status is a configured<br/>terminal status?"}
    term -->|"yes"| final["Terminal handling"]
    term -->|"no"| paused{"Paused?"}
    paused -->|"yes"| waitp["Wait until Resume<br/>events are still recorded"]
    waitp --> loop
    paused -->|"no"| due{"Wake reason set?"}
    due -->|"yes"| cycle["Reasoning cycle<br/>(see the wake and reasoning model)"]
    due -->|"no"| sleep["Sleep on a durable Temporal timer<br/>until next_wake_at<br/>or until a Signal sets a wake reason"]
    sleep --> loop

    cycle --> tool{"Validated decision<br/>requests a tool?"}
    tool -->|"yes"| exec["Record action started<br/>execute_tool Activity<br/>Record action finished + timeline entry"]
    tool -->|"no"| mem
    exec --> mem["Save memory snapshot (Activity)"]
    mem --> sched["Schedule next wake-up<br/>requested minutes, clamped to the supervisor's min and max"]
    sched --> loop

    final --> fo["Final-output Activity (LLM)<br/>deterministic fallback if it fails"]
    fo --> done["complete_run Activity<br/>final_outputs row + run status 'completed'"]
    done --> fin(["Workflow ends"])

    sigev["Signal submit_event"] -.->|"record; apply status mapping;<br/>important events set wake reason = important_event"| loop
    siginst["Signal add_run_instruction"] -.->|"store; wake reason = instruction_added"| loop
    sigctl["Signals pause, resume, interrupt"] -.->|"pause: stop reasoning<br/>resume: wake reason = resume<br/>interrupt: drop the in-flight cycle"| loop
    kill["Terminate (Temporal client hard stop, not a Signal)"] -.-> dead(["Workflow terminated"])
```

Notes that matter for reading the diagram:

- **Terminal is not an LLM decision.** A run ends when the order's status reaches one of the supervisor's configured *terminal order statuses*. The status changes when an event arrives whose type the supervisor maps to a status (for example `delivered` to `delivered`). A decision that comes back after the order became terminal is discarded.
- **A cycle runs at most one tool.** The tool is executed by the workflow through an Activity, never by the LLM.
- **Sleeping is a real Temporal timer.** Signals wake the wait early only to persist records or start a cycle; the scheduled wake-up time itself is not moved by routine events.

### Wake and reasoning model

The assignment requires three inference triggers: **workflow start**, **an important incoming event** and **a scheduled wake-up**. Two more are implemented on top: **a run-specific instruction** and **resume**. Not every event reaches the LLM.

```mermaid
flowchart TD
    ev["Incoming event<br/>Signal submit_event"] --> rec["Always: recorded in PostgreSQL + timeline entry<br/>order status updated if the supervisor maps this event"]
    rec --> pol{"Wake policy: is the event type in the<br/>supervisor's important_event_types?<br/>and not paused, not terminal,<br/>no wake already queued"}
    pol -->|"no"| routine["Routine event<br/>no wake, next_wake_at unchanged<br/>the LLM sees it at its next wake-up"]
    pol -->|"yes"| t2["Trigger 2: important event"]

    subgraph REQ["The assignment's three triggers"]
        t1["Trigger 1: workflow start"]
        t2
        t3["Trigger 3: scheduled wake-up<br/>durable timer expires"]
    end

    subgraph EXTRA["Also implemented"]
        t4["Run instruction added<br/>not while paused or terminal"]
        t5["Resume after Pause"]
    end

    t1 --> wake["Wake reason set"]
    t2 --> wake
    t3 --> wake
    t4 --> wake
    t5 --> wake

    wake --> act["Reasoning Activity<br/>LLM returns structured JSON only<br/>input: compact memory, new and recent events,<br/>supervisor + run instructions, enabled tools"]
    act --> chk["Workflow checkpoint<br/>discard the decision if interrupted, paused or terminal<br/>re-validate tool, inputs and enabled tools, one tool at most"]
    chk --> dec["Structured decision"]
    dec --> d1["tool: none or one of the enabled tools"]
    dec --> d2["next wake: requested minutes, clamped to min and max"]
    dec --> d3["memory update: situation summary + open concerns"]
    dec --> d4["assessment: recorded as a decision entry on the timeline"]

    rec -.->|"if the new order status is a terminal status"| term["Terminal order status reached"]
    term --> fin["Final-output Activity<br/>no further reasoning cycle"]
```

### Representative demo flow

This is the sealed **S2 "delayed shipment"** scenario. It is a representative flow, **not a hard-coded path**: the events are supplied by the simulator or an operator, and in a live run the LLM chooses the tools. The tools act on the mock operational tables, which the simulator prepares in the test; see [Setup](#setup) for how to prepare them for a UI-driven run. The supervisor used here lists `shipment_delayed` and `customer_message_received` as important events.

```mermaid
sequenceDiagram
    autonumber
    participant Op as Simulator or operator
    participant API as FastAPI
    participant WF as OrderWorkflow
    participant LLM as Reasoning Activity (LLM)
    participant T as Mock tools (PostgreSQL state)

    Note over Op,T: In the sealed S2 test the simulator first changes the mock tables<br/>(order, shipment, delay) and then sends each event.<br/>The UI's event panel only sends the event.
    Note over API,WF: Every event and instruction reaches the workflow as a Signal through FastAPI<br/>(POST /events, POST /instructions). The Signals are drawn straight to the workflow for brevity.
    Op->>API: start a run for the order
    API->>WF: start workflow order-{order_id}
    WF->>LLM: wake reason workflow_start
    LLM-->>WF: no tool, memory update, next wake in 60 min
    Note over WF: sleeps on a durable timer

    Op->>WF: order_created, payment_confirmed, shipment_created
    Note over WF: recorded, order status follows the mapping<br/>no wake, timer unchanged

    Op->>WF: add run instruction "If shipment is delayed, escalate immediately."
    WF->>LLM: wake reason instruction_added
    LLM-->>WF: no tool, memory update

    Op->>WF: shipment_delayed (important)
    WF->>LLM: wake reason important_event
    LLM-->>WF: tool escalate_shipment with reason and priority
    WF->>T: execute escalate_shipment
    T-->>WF: shipment marked escalated

    Op->>WF: customer_message_received "Where is my order?" (important)
    WF->>LLM: wake reason important_event
    LLM-->>WF: tool send_customer_update, next wake in 45 min
    WF->>T: execute send_customer_update
    T-->>WF: outbound message stored

    Note over WF: durable timer fires
    WF->>LLM: wake reason scheduled_wakeup
    LLM-->>WF: tool get_shipment_status
    WF->>T: execute get_shipment_status
    T-->>WF: shipment state read

    Op->>WF: delivered
    Note over WF: order status reaches the terminal status "delivered"
    WF->>LLM: final-output Activity
    LLM-->>WF: summary, key actions, key learnings, recommendations
    Note over WF: complete_run persists the final output and the workflow ends
```

---

## Key design decisions

- **One workflow per order.** The order is the natural unit of state. The workflow id is `order-{order_id}`. PostgreSQL enforces one run per order (a second run for the same order id is refused with `RUN_ALREADY_EXISTS`), and Temporal will not start a second *running* workflow with the same id.
- **Why Temporal.** The supervisor must stay alive for a long time, sleep without a process holding it, wake on a timer or a Signal, and survive worker restarts. Durable timers, Signals and replay give this without hand-written scheduling or state recovery.
- **Why Signals.** Events, run instructions and the pause, resume and interrupt controls change a live workflow, so they are Signals. Signal handlers are synchronous and side-effect free: they only update state and queue records that the main loop persists. *Terminate* is the one exception: it is Temporal's client-side hard stop, which workflow code cannot observe.
- **Why Activities.** The workflow must be deterministic, so every LLM call, database write and tool call is an Activity with its own timeout and retry policy. Persistence Activities are idempotent (ids come from the workflow, inserts use `ON CONFLICT`) and are retried; read-only tools are retried; tools with side effects get exactly one attempt so a retry can never duplicate an escalation or a customer message.
- **Why PostgreSQL is separate from Temporal.** Temporal's history is for orchestration and replay, not for querying. PostgreSQL holds the queryable application records the UI reads; the workflow keeps only compact operational state (for example the 20 most recent events).
- **Why memory and timeline are separate.** *Memory* is the supervisor's compact working state, rewritten each cycle and given to the LLM. The *timeline* is the human-readable history for observation. The LLM never receives the full history.
- **Why the LLM performs no direct side effects.** It returns a structured decision. The workflow does not trust it: it re-checks the tool against the fixed registry and the supervisor's enabled tools, enforces a single tool and its required inputs, clamps the wake interval and caps memory size, and only then runs the tool through an Activity.
- **Why the wake policy is rule-based.** "Signal" means *something happened*; the wake policy decides *whether the LLM should wake now* from the supervisor's list of important event types. It is deterministic and cheap, and every event is recorded whatever the answer.
- **Why the tools are mocked.** The assignment does not require real integrations. The four tools read and change mock order, shipment and customer-message tables in PostgreSQL, so their effects are real, observable and testable.
- **Why FakeLLM exists.** It makes the workflow testable and repeatable without a network or an API key: the scenario tests and the runtime validation use scripted decisions.
- **Why Gemini is optional.** The application runs without a key (reasoning then fails cleanly, see [Limitations](#limitations)). Gemini is the real provider that was validated with a live smoke test; it is reached through Google's OpenAI-compatible endpoint with the `openai` SDK.
- **Why no multi-agent, LangGraph or RAG.** The assignment scopes these out. One bounded reasoning call per wake, with compact memory and a fixed set of tools, is enough and is easier to reason about, test and observe.

---

## Project structure

```
.
├── README.md                     this file
├── .env.example                  configuration template (identical to backend/.env.example)
├── DOCS/                         assignment, implementation rules, architecture specification
├── backend/
│   ├── requirements.txt          pinned dependency ranges
│   ├── alembic.ini
│   ├── migrations/versions/      3 Alembic migrations (core tables, activity fields, mock operational tables)
│   ├── scripts/
│   │   ├── runtime_validation.py real Temporal runtime validation (FakeLLM), see Testing
│   │   ├── llm_smoke.py          live Gemini smoke check (2 real calls)
│   │   └── gemini_e2e.py         Temporal + LLM end-to-end scenario (real Gemini or --mock-llm)
│   ├── app/
│   │   ├── main.py               FastAPI app factory and entrypoint
│   │   ├── config.py             settings from environment / .env
│   │   ├── api/                  routers: supervisors, runs (events, instructions, controls), observation, health
│   │   ├── db/                   SQLAlchemy models, session, mock operational models
│   │   ├── temporal/
│   │   │   ├── workflows.py      OrderWorkflow
│   │   │   ├── worker.py         worker entrypoint and Activity wiring
│   │   │   ├── activities/       persistence.py, reasoning.py, tools.py
│   │   │   ├── contracts.py      Activity names and data contracts
│   │   │   ├── decision_rules.py deterministic validation of an LLM decision
│   │   │   ├── wake_policy.py    which events wake the supervisor
│   │   │   ├── supervisor_config.py  supervisor/run rows -> workflow input
│   │   │   └── types.py, constants.py
│   │   ├── llm/                  client (Gemini / OpenAI), prompts, output schemas, FakeLLM
│   │   ├── tools/                tool registry and the database-backed mock tools
│   │   └── simulation/           the external-world simulator and scenarios S1 to S5 (used by tests)
│   └── tests/                    pytest suite (313 tests)
└── frontend/
    ├── README.md                 frontend-specific notes
    ├── .env.example              API_BASE_URL
    └── src/
        ├── app/                  App Router pages: dashboard, supervisors, runs
        │   └── runs/[runId]/     run page, Server Actions, human controls, observation sections
        ├── api/                  the only code that talks HTTP to the backend, plus error messages
        └── components/           shared UI (badges, forms, LiveRefresh polling component)
```

---

## Prerequisites

| Requirement | Version | Source |
|---|---|---|
| Python | 3.9 (verified with 3.9.6; newer versions were not tried) | `backend/requirements.txt` keeps the project on Python 3.9 |
| Node.js and npm | Node 20.9 or newer (verified with Node 24.19.0, npm 11.17.0) | the `engines` field of Next.js 16.3.6 |
| PostgreSQL | verified with 17.11 (the seed SQL in Setup step 7 needs 13 or newer for `gen_random_uuid()`) | tests and the app use a real PostgreSQL |
| Temporal dev server | see [Running the application](#running-the-application) | needed to run the worker and API against a live Temporal |
| LLM API key | optional | only needed to see real reasoning, see [Environment configuration](#environment-configuration) |

Versions used for verification (installed in `.venv` and `frontend/node_modules`): FastAPI 0.128.8, uvicorn 0.39.0, SQLAlchemy 2.0.54, asyncpg 0.31.0, Alembic 1.16.5, `temporalio` 1.18.2, `openai` 2.48.0, Pydantic 2.13.5, pytest 8.4.2; Next.js 16.3.6, React 19.2.8, Tailwind CSS 4.3.3, TypeScript 5.9.3. The dependency ranges in `backend/requirements.txt` and `frontend/package.json` are the source of truth.

---

## Setup

All commands are run from the repository root unless stated.

### 1. Python environment and backend dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
```

### 2. PostgreSQL database

Create an empty database and point `DATABASE_URL` at it (see [Environment configuration](#environment-configuration)):

```bash
createdb order_supervisor
```

The default `DATABASE_URL` uses the role `postgres` with password `postgres`. On a Homebrew PostgreSQL install the superuser is usually your operating-system user and there is no `postgres` role, so use a URL without a password, for example `postgresql+asyncpg://YOUR_OS_USER@localhost:5432/order_supervisor`.

### 3. Configuration

```bash
cp backend/.env.example backend/.env
```

Then edit `backend/.env` (see the next section). `backend/.env` is git-ignored: real values, including API keys, belong only there.

### 4. Database migrations

```bash
cd backend
alembic upgrade head
cd ..
```

This creates the 11 application tables (verified on an empty database). `alembic current` should report `821bc1bc7abf (head)`.

### 5. Frontend dependencies

```bash
cd frontend
npm install
cd ..
```

Optionally `cp frontend/.env.example frontend/.env.local` if the backend is not at `http://127.0.0.1:8000`.

### 6. Temporal

See [Running the application](#running-the-application), terminal 1.

### 7. Mock operational data for the tools (needed for successful tool results)

The tools act on the mock tables `mock_orders`, `mock_shipments` and `mock_customer_messages`. Only the simulator and the test factories create those rows; **starting a run from the UI does not**. For an order that has no mock row, every tool call is recorded as a **failed** tool execution with the error `Order not found` (the supervisor still runs and can reason about the failure). To see tools succeed in a UI-driven demo, seed an order **before** starting the run, using the same order id:

```bash
psql order_supervisor <<'SQL'
INSERT INTO mock_orders (id, order_id, status, customer_id)
VALUES (gen_random_uuid(), 'DEMO-1001', 'shipped', 'CUSTOMER-1001');
INSERT INTO mock_shipments (id, order_id, shipment_id, status, tracking_number)
VALUES (gen_random_uuid(), 'DEMO-1001', 'SHIP-1001', 'in_transit', 'TRACK-1001');
SQL
```

(Adjust the `psql` connection to match your `DATABASE_URL`; `gen_random_uuid()` is built into PostgreSQL 13 and newer.) With these rows, `get_order_status`, `get_shipment_status`, `escalate_shipment` and `send_customer_update` all succeed (verified against the real tool handlers). Two things to know:

- The events you inject in the UI are Signals to the workflow only; **they do not change these mock tables**. What a tool reads is whatever the rows say. To make the world "move", update the rows yourself, for example `UPDATE mock_shipments SET status = 'delayed', delay_reason = 'Carrier capacity shortage' WHERE order_id = 'DEMO-1001';`.
- The order status shown on the run page comes from the supervisor's own *event to status mapping*, which is separate from `mock_orders.status`.

---

## Environment configuration

The backend reads settings from environment variables and from `.env` in the working directory or `backend/.env`. Using `backend/.env` works whichever directory you start the backend from.

| Variable | Default | Meaning |
|---|---|---|
| `APP_ENV` | `development` | `development`, `staging`, `production` or `test` |
| `DEBUG` | `false` | debug mode; auto-reload only when started with `python -m app.main` |
| `HOST`, `PORT` | `127.0.0.1`, `8000` | bind address used by `python -m app.main` (the `uvicorn` command takes `--port`) |
| `DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@localhost:5432/order_supervisor` | async PostgreSQL URL (asyncpg driver) |
| `TEMPORAL_ADDRESS` | `localhost:7233` | Temporal server address (API and worker) |
| `TEMPORAL_NAMESPACE` | `default` | Temporal namespace |
| `LLM_PROVIDER` | `openai` when unset (the code default); the `.env.example` template sets `gemini` | `gemini` or `openai` |
| `GEMINI_API_KEY` | none | key for `LLM_PROVIDER=gemini` |
| `LLM_MODEL` | `gemini-3.6-flash` for Gemini; required for OpenAI | model name |
| `LLM_BASE_URL` | Gemini's OpenAI-compatible endpoint for Gemini | override the provider URL |
| `LLM_REASONING_EFFORT` | `low` for Gemini | reasoning effort passed to the provider |
| `LLM_TIMEOUT_SECONDS` | `45` | per-request LLM timeout (kept below the 60 s reasoning Activity timeout) |
| `LLM_API_KEY` | none | key for `LLM_PROVIDER=openai` (together with `LLM_MODEL`) |

Frontend (`frontend/.env.local`, optional):

| Variable | Default | Meaning |
|---|---|---|
| `API_BASE_URL` | `http://127.0.0.1:8000` | FastAPI base URL. Read on the Next.js server only; never sent to the browser. |

### Which LLM configuration to use

| Situation | Configuration |
|---|---|
| **No key** (a fresh clone) | Leave the key empty. The app, the UI, events, instructions and human controls all work, but every reasoning cycle fails cleanly: the timeline records a system entry ("Reasoning failed after retries..."), no tool runs, and a final output is produced by a deterministic **fallback** built from the recorded state. You will not see real supervisor decisions. |
| **Gemini** (real reasoning) | `LLM_PROVIDER=gemini` and `GEMINI_API_KEY=<your key>` in `backend/.env`. The model, base URL and reasoning effort default to `gemini-3.6-flash`, Google's OpenAI-compatible endpoint and `low`. |
| **OpenAI** | `LLM_PROVIDER=openai`, `LLM_API_KEY` and `LLM_MODEL` (OpenAI Responses API). Implemented and covered by offline tests only; no live OpenAI call was part of this project's validation. |
| **FakeLLM** | **Not selectable through configuration.** It is a deterministic scripted test double injected only by the test suite and by `runtime_validation.py`. |

The code default provider is `openai`, so nothing changes unless another provider is selected. The `.env.example` template selects `gemini` explicitly (the provider that was validated live), so copying it and adding `GEMINI_API_KEY` is enough. If you delete the `LLM_PROVIDER` line the default `openai` applies and `GEMINI_API_KEY` alone is not used.

Never commit a real key.

---

## Running the application

The four processes run in four terminals. **Start them in this order**: the API connects to Temporal once, at startup (see [Troubleshooting](#troubleshooting)). In every backend terminal activate the environment first: `source .venv/bin/activate`.

| Terminal | What | Command |
|---|---|---|
| 1 | **Temporal dev server** (gRPC on `localhost:7233`, web UI on `http://localhost:8233`) | `temporal server start-dev` |
| 2 | **Worker**: hosts `OrderWorkflow` and its Activities | `cd backend && python -m app.temporal.worker` |
| 3 | **FastAPI** on `http://127.0.0.1:8000` (interactive docs at `/docs`) | `cd backend && uvicorn app.main:app --port 8000` |
| 4 | **Next.js** on `http://localhost:3000` | `cd frontend && npm run dev` |

Notes:

- **Terminal 1.** This is the Temporal CLI's development server (install it from Temporal's documentation). It keeps its data **in memory only**: restarting it forgets every running workflow. `temporal server start-dev` itself was not run on the authoring machine, where the CLI was not installed; the identical invocation (`server start-dev`, port 7233, UI port 8233) was verified through the Temporal binary that the Python SDK downloads and manages. If you do not have the CLI, `python backend/scripts/runtime_validation.py server` (from the repository root) starts that SDK-managed dev server; it downloads the binary on first use, so it needs network access once. That script is a validation utility, described under [Testing and verification](#testing-and-verification); it is not a required step.
- **Terminal 2.** The worker builds the LLM client from your configuration and connects to PostgreSQL. It prints nothing when it starts. If runs never make progress, see [Troubleshooting](#troubleshooting).
- **Terminal 3.** `python -m app.main` is an alternative that reads `HOST`, `PORT` and `DEBUG` from the configuration. Health check: `curl http://127.0.0.1:8000/health` returns `{"status":"ok","app_env":"development"}`.
- **Terminal 4.** For a production-style run use `npm run build && npm start` instead of `npm run dev`.

Open `http://localhost:3000`.

---

## Using the UI

- **Dashboard (`/`).** Runs are split into *Active runs* and *Completed and ended runs* (completed, terminated or failed). The **Run status** column is the application's own record: a **paused supervisor still shows as `running`** here. Buttons at the top start the two creation flows.
- **Create a supervisor (`/supervisors/new`).** Name, description, **base instruction** (how the supervisor behaves for every run), the tools it may use (`get_order_status`, `get_shipment_status`, `escalate_shipment`, `send_customer_update`), the **events that wake it immediately** (the important events), the minimum, default and maximum wake interval in minutes, the **terminal order statuses** that end a run, and, under *Advanced*, which order status each event sets. A supervisor is immutable: creating one with an existing name creates the next version. The form then takes you to *Start run* with the new supervisor selected.
- **Start a run (`/runs/new`).** An order id (one run per order), a supervisor and optional **run-specific instructions** (one per line). The supervisor list only shows supervisors that existing runs use and the one you just created, because the backend has no supervisor-list endpoint; you can also paste a supervisor id. The run page opens with the workflow already running.
- **The run page (`/runs/{run id}`)**, top to bottom:
  1. **Run overview** and the header showing both statuses: **Run** (the application's record) and **Workflow** (the live Temporal state).
  2. **Workflow status**: state (`reasoning`, `sleeping`, `paused` or `terminal`), last cycle outcome, last wake reason, next scheduled wake, reasoning cycles, interrupts, events received. It is read live from Temporal; if that cannot be read it says so instead of guessing.
  3. **Human controls** (active runs only), described below.
  4. **Instructions**: the supervisor's read-only base instruction, then this run's **additional instructions** with a form to add one (for example "If shipment is delayed, escalate immediately."). Adding one normally wakes the supervisor; on a paused run it is recorded but reasoning stays paused until Resume.
  5. **Inject an event**: choose one of the ten event types, optionally with a JSON object as details. The supervisor records every event; only events its wake policy marks important wake it immediately, and an event can change the order status. It does not change the mock operational tables the tools read (see [Setup](#setup), step 7).
  6. **Observation**: **Memory** (the supervisor's compact working memory, not the full history), **Timeline** (oldest first), **Actions**, **Tool executions** (input, result, error) and **Final output** (summary, key actions, key learnings, recommendations and whether an LLM or the fallback wrote it).
- **Human controls.** Each request goes through the backend to Temporal.
  - **Pause**: keep the workflow alive but stop supervisor reasoning. Events are still recorded and can be evaluated after Resume. It does not change the run status.
  - **Resume**: allow reasoning to continue; the supervisor re-evaluates.
  - **Interrupt**: stop the current reasoning cycle without terminating the workflow. If an LLM decision is in flight it is discarded; a tool that already started is allowed to finish and is recorded.
  - **Terminate**: hard-stop the workflow. It needs an explicit confirmation and cannot be undone. It is not Pause and not Interrupt.
  - If the workflow state cannot be read the buttons are shown disabled ("Unable to determine the workflow state").
- **Live updates.** While a run is active its page refreshes itself every 3 seconds (a small indicator next to the **Refresh** button says so) and stops when the run completes or is terminated or the workflow is closed; a paused run keeps updating. Refresh reads everything at once. There are no WebSockets or server-sent events.

---

## API overview

FastAPI serves interactive docs at `http://127.0.0.1:8000/docs`. All errors use `{"error": <message>, "code": <CODE>}`. A `202` means the request was **accepted at the workflow boundary**, not yet processed.

| Method and path | Purpose |
|---|---|
| `GET /health` | health check |
| `POST /api/supervisors` | create a supervisor (same name creates the next version) |
| `GET /api/supervisors/{supervisor_id}` | read a supervisor |
| `POST /api/runs` | create a run and start its workflow (`201`) |
| `GET /api/runs` | list runs |
| `GET /api/runs/{run_id}` | read a run |
| `POST /api/runs/{run_id}/events` | send an order event into the workflow as a Signal (`202`) |
| `POST /api/runs/{run_id}/instructions` | add a run-specific instruction (Signal, `202`) |
| `POST /api/runs/{run_id}/pause`, `/resume`, `/interrupt` | human controls (Signals, `202`) |
| `POST /api/runs/{run_id}/terminate` | hard-stop the workflow (Temporal client terminate, `202`) |
| `GET /api/runs/{run_id}/status` | the workflow's live state (Temporal Query); `409` when the workflow is closed |
| `GET /api/runs/{run_id}/timeline`, `/memory`, `/actions`, `/tool-executions`, `/final-output` | recorded history from PostgreSQL, oldest first |

Common error codes: `VALIDATION_ERROR` (400), `RUN_NOT_FOUND` (404), `RUN_ALREADY_EXISTS` (409), `RUN_NOT_ACTIVE` (409), `TEMPORAL_UNAVAILABLE` (503).

The ten event types are `order_created`, `payment_confirmed`, `payment_failed`, `shipment_created`, `shipment_delayed`, `delivered`, `refund_requested`, `customer_message_received`, `no_update_for_n_hours` and `order_cancelled`.

---

## Testing and verification

Backend (needs the migrated PostgreSQL from Setup; it does **not** need a running Temporal server, the tests use Temporal's time-skipping test server):

```bash
source .venv/bin/activate
pytest backend/tests -q            # 313 tests
```

The suite covers the API, the models, the workflow, the Activities, the LLM client and schemas, the mock tools, and five end-to-end scenarios (S1 smooth delivery, S2 delayed shipment, S3 LLM unavailable, S4 human controls, S5 payment failure and cancellation) that run against Temporal's time-skipping test server with a scripted FakeLLM. The Temporal SDK may download its test-server binary the first time.

Frontend (from `frontend/`):

```bash
npm run lint
npx tsc --noEmit                   # typecheck
npm run build                      # production build
```

Runtime and provider validation (manual utilities, **not** part of the pytest suite and **not** needed to run the app):

```bash
# Real Temporal dev server + real worker + FastAPI + PostgreSQL, scripted FakeLLM (no API key, no cost)
python backend/scripts/runtime_validation.py preflight
python backend/scripts/runtime_validation.py all            # runs the S1 scenario on real processes, then cleans up
python backend/scripts/runtime_validation.py cleanup        # only if a previous run was interrupted

# Live Gemini check: 2 real requests through the runtime's own LLM client (needs GEMINI_API_KEY)
python backend/scripts/llm_smoke.py

# Production worker + real Temporal, driven against a local OpenAI-compatible stub (no quota)
python backend/scripts/gemini_e2e.py --mock-llm
```

What has been verified: the backend suite (313 passing); the real Temporal runtime validation of S1 with the scripted FakeLLM; the live Gemini smoke test (reasoning decision and final output both valid); and a `--mock-llm` rehearsal of the full pipeline with the production worker. The frontend flows (creation, event injection, instructions, human controls, observation and polling) were exercised in a real browser against the real stack with FakeLLM. The frontend has no automated test suite of its own.

---

## What is real vs mocked

| Component | Status |
|---|---|
| PostgreSQL | **Real.** All application data; the test suite also runs against it. |
| Temporal runtime | **Real.** Validated with a real dev server, a real worker and separate processes (`runtime_validation.py`). The scenario tests use Temporal's time-skipping test server. |
| FastAPI | **Real.** |
| Next.js UI | **Real.** |
| Operational tools | **Mocked.** Four tools that read and change mock order, shipment and customer-message tables in PostgreSQL; nothing external is contacted. The mock rows are created by the simulator and tests or seeded by hand, not by the UI. |
| FakeLLM | **Deterministic test double.** Used by the tests and the runtime validation; not selectable through configuration. |
| Gemini | **Real provider, smoke-tested.** A live smoke test passed (2 real calls: a reasoning decision and a final output, both valid). |
| Full Gemini + Temporal end to end | **Not verified.** The `--mock-llm` rehearsal proves the pipeline and the exact HTTP request shape, but not Gemini itself; a complete real run with a real key has not been performed. |
| OpenAI provider | Implemented; covered by offline tests only. |

---

## Limitations

- **Proof of concept.** No authentication, no multi-tenancy and no production hardening. The backend has no CORS configuration on purpose: the browser talks only to Next.js.
- **Mocked operations.** No real commerce, shipping or messaging integrations. The mock operational state is not driven by the UI: an order started from the UI has no mock rows until you seed them (Setup, step 7), and injected events do not change them, so tools on an unseeded order fail with `Order not found`.
- **LLM dependency.** Real supervisor decisions need a provider key. Without one every reasoning cycle fails cleanly and the final output comes from the deterministic fallback. Only the Gemini path has been validated against a live provider.
- **Simple wake policy.** Whether an event wakes the supervisor is a fixed, rule-based list per supervisor. There is no LLM classifier, no agent-written wake guidance and no special handling of unknown events (event types are a fixed, validated vocabulary).
- **One tool per reasoning cycle**, from a fixed set of four.
- **Long histories.** The workflow does not use `continue_as_new`; it keeps only compact state (for example the 20 most recent events) but a very long-running workflow's history is not rolled over.
- **Temporal dev server is in-memory.** Restarting it loses running workflows, while PostgreSQL still shows their runs as `running`. Use a Temporal server with persistence for anything beyond a demo.
- **PostgreSQL and Temporal are not one transaction.** Creating a run saves the row, then starts the workflow, then records the outcome; failures between the steps are reported honestly (a run that failed to start keeps its order id).
- **UI scope.** The supervisor picker only lists supervisors used by existing runs (the backend has no supervisor-list endpoint). Live updates re-render the whole page every 3 seconds; if the backend goes down the page shows an error panel and a half-typed form draft is lost. The timeline is oldest first with no filtering or pagination. Only the desktop layout was reviewed.
- **Verification scope.** The frontend has no automated tests; the full Gemini + Temporal end-to-end has not been run (see the table above).

---

## Troubleshooting

- **`role "postgres" does not exist`.** Your PostgreSQL has no `postgres` role. Set `DATABASE_URL` to use your own user, for example `postgresql+asyncpg://YOUR_OS_USER@localhost:5432/order_supervisor`.
- **Creating a run says `TEMPORAL_UNAVAILABLE` (503), or the run page says "Workflow status unavailable".** Temporal is not running, or the API was started before it. The API connects to Temporal only once, when it starts, and never retries: start Temporal first, then **restart the API**. A run whose workflow could not start is marked `failed` and keeps its order id, so start a new run with a different order id.
- **A run stays `running` but nothing happens.** The worker is not running. Start it in terminal 2; Temporal delivers the queued work when it connects.
- **Runs exist but show no supervisor decisions; the timeline shows "Reasoning failed".** No usable LLM key is configured (see [Which LLM configuration to use](#which-llm-configuration-to-use)). Set `LLM_PROVIDER=gemini` and `GEMINI_API_KEY` in `backend/.env` and restart the **worker**.
- **After restarting Temporal, old runs still say `running`.** The dev server keeps state in memory only, so those workflows are gone. Start new runs.
- **Tool executions show `failed` with `Order not found` (or `Shipment not found`).** The order has no mock operational rows. Seed them as described in [Setup](#setup), step 7, before starting the run.
- **Backend tests fail to connect.** Check that PostgreSQL is running, `DATABASE_URL` is correct and `alembic upgrade head` has been applied.
- **`npm run dev` creates `frontend/AGENTS.md` and `frontend/CLAUDE.md`.** The Next.js 16 development server writes these two files on first start. They are not part of the project and can be deleted.
- **Ports.** Temporal uses 7233 (gRPC) and 8233 (UI), FastAPI 8000 and Next.js 3000.

---

## Demo walkthrough

A short path through the product (a real LLM key is needed to see decisions, see [Environment configuration](#environment-configuration)):

1. Start the four processes as described in [Running the application](#running-the-application) and open `http://localhost:3000`.
2. **Seed the mock order** ([Setup](#setup), step 7) and **create a supervisor** with the tools enabled and, for example, `shipment_delayed` and `customer_message_received` as important events.
3. **Start a run** for the seeded order. The first reasoning cycle (workflow start) appears on the run page, and the workflow goes to sleep on a durable timer.
4. **Inject events** in the order of the [representative flow](#representative-demo-flow): the routine events (`order_created`, `payment_confirmed`, `shipment_created`) are recorded without waking the supervisor; `shipment_delayed` is important and wakes it.
5. **Add a run instruction** such as "If shipment is delayed, escalate immediately." and watch the supervisor react.
6. Use the **human controls**: Pause (events are recorded but there is no reasoning), Resume, Interrupt. Terminate is available but ends the workflow for good.
7. Inject `delivered`: the order reaches its terminal status, and the **final output** (summary, key actions, key learnings, recommendations) appears.

The detailed walkthrough script for the video is kept separately.
