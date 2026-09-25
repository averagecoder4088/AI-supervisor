# Order Supervisor

A proof of concept for a long-running AI supervisor that oversees **one order** from creation to completion.

- Every order gets its own durable [Temporal](https://temporal.io) workflow.
- Order events reach the workflow as Temporal Signals.
- An LLM decides when to act, when to sleep and when to wake up again.
- Tools carry out the actions, and a final report is written when the order ends.

**Stack:** Next.js (App Router) + Tailwind CSS · FastAPI · Temporal Python SDK · PostgreSQL · an LLM behind a small client interface (Gemini through its OpenAI-compatible endpoint, or OpenAI).

**Documents:** the assignment is [`DOCS/PROBLEM_STATEMENT.md`](DOCS/PROBLEM_STATEMENT.md), the final architecture is [`ARCHITECTURE_FINAL.md`](ARCHITECTURE_FINAL.md), the original specification is [`DOCS/Order_Supervisor_Final_Architecture_Specification (1).docx`](DOCS/Order_Supervisor_Final_Architecture_Specification%20%281%29.docx), and the video recording script is [`DOCS/WALKTHROUGH_SCRIPT.md`](DOCS/WALKTHROUGH_SCRIPT.md). This README describes the system **as it is implemented**.

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
9. [Live demo simulator](#live-demo-simulator)
10. [API overview](#api-overview)
11. [Testing and verification](#testing-and-verification)
12. [What is real vs mocked](#what-is-real-vs-mocked)
13. [Limitations](#limitations)
14. [Troubleshooting](#troubleshooting)
15. [Demo walkthrough](#demo-walkthrough)

---

## Architecture

The responsibilities are split on purpose:

| Part | Responsibility |
|---|---|
| **Temporal** | Durable orchestration: the workflow's lifecycle, Signals, timers and compact state. |
| **`OrderWorkflow`** | Decides *when* the supervisor reasons and *what is allowed to happen*. Deterministic: no I/O, no randomness, time only through Temporal. |
| **Activities** | All non-deterministic or external work: LLM calls, PostgreSQL writes, tool execution. |
| **PostgreSQL** | Application and history data (supervisors, runs, events, timeline, actions, tool executions, memory snapshots, final output) plus the mock operational tables the tools act on. |
| **LLM** | Structured reasoning. Returns a JSON decision and has **no side effects**. |
| **Tools** | Four mocked tools over mock order, shipment and message rows: `get_order_status`, `get_shipment_status` (read-only), `escalate_shipment`, `send_customer_update` (side effects). |
| **FastAPI** | A thin boundary over PostgreSQL and the Temporal client. |
| **Next.js** | The UI. The browser only talks to Next.js; Server Components read from FastAPI and Server Actions write to it. |

- The next wake time is part of the LLM's decision and a run closes when the order reaches a terminal status, so there is no separate schedule or close tool.
- The mock rows are created by the simulator and the tests, or seeded by hand (see [Setup](#setup), step 7). The API and the UI do not create them.

### System architecture

Where the components live and who is responsible for what. Read it top to bottom.

```mermaid
flowchart TB
    browser["Browser"]
    next["Next.js App Router<br/>Server Components, Server Actions"]
    api["FastAPI<br/>REST API"]
    temporal["Temporal<br/>durable orchestration"]
    wf["Worker: OrderWorkflow<br/>deterministic, one per order"]
    act["Activities<br/>non-deterministic work"]
    pg[("PostgreSQL<br/>application and history data")]
    llm["LLM<br/>reasoning only"]
    tools["Mock Tools<br/>mock_* tables"]

    browser -->|"HTTP"| next
    next -->|"REST, server-side"| api
    api -->|"start, Signal, Query, terminate"| temporal
    temporal -->|"task queue"| wf
    wf -->|"executes"| act
    act -->|"persists"| pg
    act -->|"reasons"| llm
    act -->|"executes tool"| tools
    api -.->|"reads history, writes runs"| pg

    classDef ui fill:#dbeafe,stroke:#2563eb,color:#111
    classDef plain fill:#f1f5f9,stroke:#64748b,color:#111
    classDef orch fill:#e0e7ff,stroke:#4f46e5,color:#111
    classDef work fill:#dcfce7,stroke:#16a34a,color:#111
    classDef data fill:#fef9c3,stroke:#ca8a04,color:#111
    class browser,next ui
    class api plain
    class temporal,wf orch
    class act work
    class pg,llm,tools data
```

Colours: blue = browser and Next.js, grey = FastAPI, indigo = Temporal and the workflow, green = Activities, yellow = data and external services.

- The browser never talks to FastAPI, Temporal or PostgreSQL directly.
- **FastAPI** creates supervisors and runs, **starts** the workflow once (in `POST /api/runs`), and afterwards only **signals, queries or terminates** it. It reads recorded history straight from PostgreSQL (dotted line) and never writes events itself.
- **Temporal** hosts the deterministic `OrderWorkflow` (id `order-{order_id}`) in the worker process (`python -m app.temporal.worker`).
- **Activities** persist to PostgreSQL, call the LLM and run the tools. PostgreSQL is not the workflow engine.

### Order workflow lifecycle

How one workflow progresses. The loop never spins: it either reasons once or waits on a durable timer.

```mermaid
flowchart TB
    create(["POST /api/runs"]) --> row1[("Run row saved<br/>starting")]
    row1 --> start["start_workflow<br/>the only start path"]
    start -.->|"start fails"| rowf[("Run row<br/>failed")]
    start -->|"started"| row2[("Run row<br/>running")]
    start --> init["Initialize<br/>@workflow.init"]
    init -->|"1. workflow start"| reason["Reason<br/>LLM Activity"]
    signals["Signals<br/>to the running workflow"] -->|"2. important event<br/>or instruction<br/>(not while paused)"| reason
    wait["Wait<br/>durable timer"] -->|"3. timer fires"| reason
    reason --> tool["Optional tool"]
    tool --> memory["Update memory"]
    memory --> schedule["Schedule next wake"]
    schedule --> wait

    signals -.->|"Pause"| paused["Paused<br/>no reasoning, events still recorded<br/>run row stays running"]
    paused -->|"Resume"| reason

    signals -.->|"event sets a<br/>terminal status"| terminal["Terminal status reached<br/>not an LLM decision"]
    reason -.->|"stale"| discard["Decision discarded<br/>order became terminal"]
    discard -.-> terminal
    terminal --> flush1["Flush queued records"]
    flush1 --> final["Final output<br/>LLM, or deterministic fallback"]
    final --> flush2["Flush late records"]
    flush2 --> complete["complete_run Activity<br/>saves the final output"]
    complete -.->|"sets"| rowc[("Run row<br/>completed")]
    complete --> drain["Final drain<br/>persist any late Signals"]
    drain --> done(["Workflow ends"])
    stop["Terminate<br/>client hard stop"] -.-> halted(["Workflow terminated"])
    halted -.->|"API then sets"| rowt[("Run row<br/>terminated")]

    classDef sig fill:#dbeafe,stroke:#2563eb,color:#111
    classDef llmc fill:#dcfce7,stroke:#16a34a,color:#111
    classDef toolc fill:#ffedd5,stroke:#ea580c,color:#111
    classDef sleep fill:#f1f5f9,stroke:#64748b,color:#111
    classDef pausec fill:#ede9fe,stroke:#7c3aed,color:#111
    classDef term fill:#cffafe,stroke:#0891b2,color:#111
    classDef stopc fill:#fee2e2,stroke:#dc2626,color:#111
    classDef stale fill:#fef3c7,stroke:#d97706,color:#111
    classDef rowc fill:#fef9c3,stroke:#ca8a04,color:#111
    classDef step fill:#ffffff,stroke:#94a3b8,color:#111
    class discard stale
    class signals sig
    class reason llmc
    class tool toolc
    class wait sleep
    class paused pausec
    class terminal,flush1,final,flush2,complete,drain term
    class stop,halted stopc
    class row1,row2,rowf,rowc,rowt rowc
    class create,start,init,memory,schedule,done step
```

Colours: blue = Signals, green = LLM reasoning, orange = a tool, grey = waiting, purple = paused, amber = a stale decision discarded, teal = terminal handling, red = hard stop, yellow cylinders = the run's status row in PostgreSQL.

- **Two lifecycles.** The Temporal workflow and the run's status row in PostgreSQL are separate.
  - The API writes `starting` before contacting Temporal, then `running`, or `failed` if the start fails.
  - The workflow's `complete_run` Activity writes `completed`; the API writes `terminated` after a hard stop.
  - Pause is workflow state only, so the row stays `running`.
  - If the workflow itself fails, nothing updates the row (a documented limitation).
- **Pause is a branch, not an end.** No reasoning and no timer wake while paused, but events are still recorded. Resume returns to the Reason step. A terminal status still ends a paused workflow.
- **Terminal handling, in order:** flush queued records, generate the final output (LLM, or the deterministic fallback), flush again, run `complete_run`, drain late Signals, end. Late Signals are persisted but cannot reopen the workflow.
- **Starting and signalling.**
  - `POST /api/runs` is the only place a workflow starts. An incoming event can never start one, and Signal-With-Start is not used.
  - State is initialised in `@workflow.init`, so a Signal delivered in the first activation still sees the configuration. One regression test covers exactly that.
  - If the run is not active, or Temporal reports the workflow closed or missing, the API answers `409 RUN_NOT_ACTIVE`.
- **Triggers.** Workflow start, an important incoming event and a scheduled wake-up (the assignment's three). A run instruction and Resume also wake the supervisor, but not while it is paused or the order is terminal.
- **Terminal is not an LLM decision.** A run ends when an event moves the order to one of the supervisor's terminal statuses (for example `delivered` to `delivered`). A decision still in flight is discarded (`discarded_terminal`) and the workflow goes straight to the final output.
- **One tool per cycle**, executed by the workflow through an Activity, never by the LLM. Sleeping is a real Temporal timer, and a routine event does not move the scheduled wake-up.
- **Human controls.** Pause, Resume and Interrupt are Signals. Terminate is Temporal's client-side hard stop, and the API then records the run as `terminated`.

### Wake and reasoning model

What causes reasoning, and what happens to the resulting decision. Not every event reaches the LLM.

```mermaid
flowchart TB
    event["Incoming event<br/>always recorded"] --> policy{"Wake policy:<br/>important?"}
    policy -->|"no: unimportant"| record["Recorded only<br/>no reasoning wake<br/>seen at the next wake-up"]
    policy -->|"yes: 2. important event"| reason
    start["1. Workflow start"] --> reason
    timer["3. Scheduled timer"] --> reason
    instr["Run instruction<br/>not while paused"] -.-> reason
    resume["Resume<br/>after Pause"] -.-> reason
    reason["Reasoning Activity<br/>LLM returns JSON only<br/>no side effects"] --> decision["Structured decision<br/>assessment, optional tool,<br/>next wake, memory update"]
    decision --> validate{"Workflow validation<br/>and checkpoint"}
    validate -->|"valid"| apply["Apply<br/>run tool, save memory,<br/>schedule next wake"]
    validate -->|"stale: interrupted<br/>or paused"| discard["Nothing applied<br/>no tool, no memory update<br/>events stay pending"]
    reason -.->|"LLM failed after retries<br/>or interrupted mid-call"| discard
    validate -.->|"stale: order terminal"| terminal["Terminal handling<br/>final output, see lifecycle diagram"]
    apply --> alive["Workflow stays alive<br/>sleeps on a durable timer,<br/>or stays paused"]
    discard --> alive

    classDef trig fill:#dbeafe,stroke:#2563eb,color:#111
    classDef llmc fill:#dcfce7,stroke:#16a34a,color:#111
    classDef okc fill:#f1f5f9,stroke:#64748b,color:#111
    classDef bad fill:#fee2e2,stroke:#dc2626,color:#111
    classDef decide fill:#fef3c7,stroke:#d97706,color:#111
    classDef term fill:#cffafe,stroke:#0891b2,color:#111
    class event,start,timer,instr,resume trig
    class reason,decision llmc
    class record,apply,alive okc
    class discard bad
    class policy,validate decide
    class terminal term
```

Colours: blue = triggers, green = the LLM's part, amber = a decision point, grey = normal outcome, red = nothing applied, teal = terminal handling.

- **Important events.** Every event is recorded. The wake policy then checks the event type against the supervisor's `important_event_types` (and that it is not paused, not terminal and no wake is already queued). A routine event leaves the timer untouched and is seen at the next wake.
- **Extra triggers.** A run instruction and Resume reach the same Reasoning Activity. An instruction added while paused is only recorded; Resume ends the pause and wakes the supervisor.
- **The LLM only returns a decision.** Its input is compact memory, new and recent events, the instructions and the enabled tools, never the full history. The decision holds an assessment, an optional tool with its input, the next wake in minutes and a memory update. The Activity checks it against a strict schema and retries a bad answer.
- **The workflow validates before acting.**
  - A decision is stale if the cycle was interrupted, the supervisor was paused, or the order became terminal meanwhile.
  - Otherwise the tool is re-checked against the fixed registry and the supervisor's enabled tools (one tool, required inputs present) and the wake time is clamped to the supervisor's minimum and maximum.
  - A rejected tool is dropped and noted on the timeline; the memory update and next wake still apply.
- **"Nothing applied" is precise.** A stale decision or a failed LLM call runs no tool, saves no memory snapshot and leaves the pending events pending. Only the cycle outcome and a system note are recorded. A failed or interrupted cycle never ends the workflow; it reasons again at its next wake (except when the decision was made stale by a terminal status).

### Representative demo flow

The sealed **S2 "delayed shipment"** scenario, checked step by step against `backend/tests/test_scenario_s2_delayed_shipment.py`.

- It is a representative flow, not a hard-coded path. In a live run the LLM chooses the tools.
- In the test the simulator changes the mock tables first and then sends each event. The UI's event panel only sends the event (see [Setup](#setup), step 7).
- The supervisor lists `shipment_delayed` and `customer_message_received` as important events. The run has six events, five reasoning cycles and one final-output call.

```mermaid
flowchart LR
    subgraph P1["1. Start"]
        direction TB
        c1(["Run created<br/>POST /api/runs"]) --> w1["Workflow starts"]
        w1 --> r1["Reason<br/>workflow start, no tool"]
        r1 --> z1["Sleep<br/>durable timer"]
        z1 --> e1["order_created, payment_confirmed,<br/>shipment_created<br/>recorded, no wake"]
        e1 --> e2["Run instruction:<br/>escalate if delayed"]
        e2 --> r2["Reason<br/>instruction added, no tool"]
    end
    subgraph P2["2. Delay"]
        direction TB
        e3["shipment_delayed<br/>important event"] --> r3["Reason"]
        r3 --> t1["Tool: escalate_shipment<br/>shipment escalated"]
        t1 --> e4["customer_message_received<br/>important event"]
        e4 --> r4["Reason"]
        r4 --> t2["Tool: send_customer_update<br/>outbound message stored"]
    end
    subgraph P3["3. Follow-up and completion"]
        direction TB
        z2["Sleep<br/>next wake in 45 min"] --> r5["Reason<br/>scheduled wake-up"]
        r5 --> t3["Tool: get_shipment_status<br/>read-only"]
        t3 --> e5["delivered<br/>terminal status, no reasoning"]
        e5 --> fo["Final output<br/>LLM Activity"]
        fo --> done(["Run completed"])
    end
    P1 --> P2
    P2 --> P3
    style P1 fill:#f8fafc,stroke:#cbd5e1
    style P2 fill:#f8fafc,stroke:#cbd5e1
    style P3 fill:#f8fafc,stroke:#cbd5e1

    classDef ev fill:#dbeafe,stroke:#2563eb,color:#111
    classDef rs fill:#dcfce7,stroke:#16a34a,color:#111
    classDef tl fill:#ffedd5,stroke:#ea580c,color:#111
    classDef sl fill:#f1f5f9,stroke:#64748b,color:#111
    classDef tm fill:#cffafe,stroke:#0891b2,color:#111
    classDef st fill:#ffffff,stroke:#94a3b8,color:#111
    class e1,e2,e3,e4 ev
    class r1,r2,r3,r4,r5 rs
    class t1,t2,t3 tl
    class z1,z2 sl
    class e5,fo,done tm
    class c1,w1 st
```

Legend: blue = an event or instruction entering the workflow, green = the supervisor reasons (LLM), orange = a tool runs, grey = sleeping on a durable timer, teal = terminal completion.

---

## Key design decisions

- **One workflow per order.** The order is the natural unit of state. The id is `order-{order_id}`. PostgreSQL enforces one run per order (`RUN_ALREADY_EXISTS`), and Temporal will not start a second running workflow with the same id.
- **Why Temporal.** The supervisor must stay alive for a long time, sleep without a process holding it, wake on a timer or a Signal, and survive worker restarts.
- **Why Signals.** Events, instructions and the pause, resume and interrupt controls change a live workflow. Handlers are synchronous and side-effect free: they update state and queue records for the main loop to persist. Terminate is the exception (client-side hard stop).
- **Why Activities.** Workflow code must be deterministic, so every LLM call, database write and tool call is an Activity with its own timeout and retry policy.
  - Persistence Activities are idempotent (ids come from the workflow, inserts use `ON CONFLICT`) and are retried.
  - Read-only tools are retried; tools with side effects get exactly one attempt, so a retry never duplicates an escalation or a customer message.
- **Why PostgreSQL is separate from Temporal.** Temporal's history is for orchestration and replay, not for querying. PostgreSQL holds the records the UI reads; the workflow keeps compact state (for example the 20 most recent events).
- **Memory vs timeline.** Memory is the supervisor's compact working state, rewritten each cycle and given to the LLM. The timeline is the readable history for observation. The LLM never receives the full history.

---

## Project structure

```
.
├── README.md                     this file
├── ARCHITECTURE_FINAL.md         final architecture description
├── .env.example                  configuration template (identical to backend/.env.example)
├── DOCS/                         assignment, implementation rules, architecture specification
├── backend/
│   ├── requirements.txt          dependency ranges
│   ├── alembic.ini
│   ├── migrations/versions/      3 Alembic migrations (core tables, activity fields, mock operational tables)
│   ├── scripts/
│   │   ├── runtime_validation.py real Temporal runtime validation (FakeLLM), see Testing
│   │   ├── llm_smoke.py          live Gemini smoke check (2 real calls)
│   │   ├── gemini_e2e.py         Temporal + LLM end-to-end scenario (real Gemini or --mock-llm)
│   │   └── simulate.py           live demo simulator: change the mock world and send the event
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
│   │   └── simulation/           external-world simulator and scenarios S1 to S5 (used by tests)
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

| Requirement | Version |
|---|---|
| Python | 3.9 (verified with 3.9.6; newer versions were not tried) |
| Node.js and npm | Node 20.9 or newer (verified with Node 24.19.0) |
| PostgreSQL | 13 or newer (verified with 17.11); the seed SQL uses `gen_random_uuid()` |
| Temporal dev server | see [Running the application](#running-the-application) |
| LLM API key | optional; only needed to see real reasoning, see [Environment configuration](#environment-configuration) |

The dependency ranges in `backend/requirements.txt` and `frontend/package.json` are the source of truth.

---

## Setup

Run all commands from the repository root unless stated.

### 1. Python environment and backend dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
```

### 2. PostgreSQL database

```bash
createdb order_supervisor
```

- The default `DATABASE_URL` uses the role `postgres` with password `postgres`.
- On a Homebrew install there is usually no `postgres` role. Use your own user instead, for example `postgresql+asyncpg://YOUR_OS_USER@localhost:5432/order_supervisor`.

### 3. Configuration

```bash
cp backend/.env.example backend/.env
```

Edit `backend/.env` (see the next section). It is git-ignored: real values, including API keys, belong only there.

### 4. Database migrations

```bash
cd backend
alembic upgrade head
cd ..
```

This creates the 11 application tables. `alembic current` should report `821bc1bc7abf (head)`.

### 5. Frontend dependencies

```bash
cd frontend
npm install
cd ..
```

Optionally `cp frontend/.env.example frontend/.env.local` if the backend is not at `http://127.0.0.1:8000`.

### 6. Temporal

See [Running the application](#running-the-application), terminal 1.

### 7. Mock operational data for the tools

The tools act on `mock_orders`, `mock_shipments` and `mock_customer_messages`. Only the simulator and the test factories create those rows; **starting a run from the UI does not**.

- For an order with no mock row, every tool call is recorded as a **failed** tool execution (`Order not found`). The supervisor still runs and can reason about the failure.
- Events injected in the UI are Signals only. **They do not change these tables**; a tool reads whatever the rows say.
- The order status on the run page comes from the supervisor's own event-to-status mapping, which is separate from `mock_orders.status`.

Two ways to create the rows **before** the run starts, with the same order id:

- **The [live demo simulator](#live-demo-simulator):** `python backend/scripts/simulate.py place DEMO-1001`. Its later commands change the mock world and send the matching event together.
- **By hand**, for a UI-only demo:

```bash
psql order_supervisor <<'SQL'
INSERT INTO mock_orders (id, order_id, status, customer_id)
VALUES (gen_random_uuid(), 'DEMO-1001', 'shipped', 'CUSTOMER-1001');
INSERT INTO mock_shipments (id, order_id, shipment_id, status, tracking_number)
VALUES (gen_random_uuid(), 'DEMO-1001', 'SHIP-1001', 'in_transit', 'TRACK-1001');
SQL
```

Adjust the `psql` connection to match your `DATABASE_URL`. To make the world move by hand, update the rows, for example `UPDATE mock_shipments SET status = 'delayed', delay_reason = 'Carrier capacity shortage' WHERE order_id = 'DEMO-1001';`

---

## Environment configuration

The backend reads environment variables and `.env` from the working directory or `backend/.env`.

| Variable | Default | Meaning |
|---|---|---|
| `APP_ENV` | `development` | `development`, `staging`, `production` or `test` |
| `DEBUG` | `false` | debug mode; auto-reload only with `python -m app.main` |
| `HOST`, `PORT` | `127.0.0.1`, `8000` | bind address used by `python -m app.main` |
| `DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@localhost:5432/order_supervisor` | async PostgreSQL URL (asyncpg driver) |
| `TEMPORAL_ADDRESS` | `localhost:7233` | Temporal server address (API and worker) |
| `TEMPORAL_NAMESPACE` | `default` | Temporal namespace |
| `LLM_PROVIDER` | `openai` in code; the `.env.example` template sets `gemini` | `gemini` or `openai` |
| `GEMINI_API_KEY` | none | key for `LLM_PROVIDER=gemini` |
| `LLM_MODEL` | `gemini-3.1-flash-lite` for Gemini; required for OpenAI | model name |
| `LLM_BASE_URL` | Gemini's OpenAI-compatible endpoint | override the provider URL |
| `LLM_REASONING_EFFORT` | not sent | sent to the provider only when set (some models reject it) |
| `LLM_TIMEOUT_SECONDS` | `45` | per-request LLM timeout (kept below the 60 s reasoning Activity timeout) |
| `LLM_API_KEY` | none | key for `LLM_PROVIDER=openai` (together with `LLM_MODEL`) |

Frontend (`frontend/.env.local`, optional): `API_BASE_URL`, default `http://127.0.0.1:8000`. Read on the Next.js server only; never sent to the browser.

### Which LLM configuration to use

| Situation | Configuration |
|---|---|
| **No key** | Leave the key empty. The app, UI, events, instructions and controls work, but every reasoning cycle fails cleanly: the timeline records "Reasoning failed after retries...", no tool runs, and the final output comes from a deterministic **fallback**. |
| **Gemini** | `LLM_PROVIDER=gemini` and `GEMINI_API_KEY=<key>` in `backend/.env`. The model defaults to `gemini-3.1-flash-lite` and no reasoning effort is sent unless `LLM_REASONING_EFFORT` is set; leave `LLM_MODEL` unset to use the default. |
| **OpenAI** | `LLM_PROVIDER=openai`, `LLM_API_KEY` and `LLM_MODEL` (Responses API). Covered by offline tests only. |
| **FakeLLM** | A scripted test double, injected only by the test suite and `runtime_validation.py`. **Not selectable through configuration.** |

- The code default provider is `openai`. The `.env.example` template selects `gemini`, so copying it and adding `GEMINI_API_KEY` is enough. If you delete the `LLM_PROVIDER` line, `openai` applies and `GEMINI_API_KEY` alone is not used.
- Never commit a real key.

---

## Running the application

Start the four processes in four terminals, **in this order**: the API connects to Temporal once, at startup. In every backend terminal run `source .venv/bin/activate` first.

| Terminal | What | Command |
|---|---|---|
| 1 | **Temporal dev server** (gRPC `localhost:7233`, web UI `http://localhost:8233`) | `temporal server start-dev` |
| 2 | **Worker**: hosts `OrderWorkflow` and its Activities | `cd backend && python -m app.temporal.worker` |
| 3 | **FastAPI** on `http://127.0.0.1:8000` (docs at `/docs`) | `cd backend && uvicorn app.main:app --port 8000` |
| 4 | **Next.js** on `http://localhost:3000` | `cd frontend && npm run dev` |

- **Terminal 1.** The Temporal CLI's development server keeps data **in memory only**: restarting it forgets every running workflow. Without the CLI, `python backend/scripts/runtime_validation.py server` starts an SDK-managed dev server instead (it downloads a binary on first use).
- **Terminal 2.** The worker builds the LLM client from your configuration. It prints nothing when it starts. Restart it after changing `backend/.env`.
- **Terminal 3.** `python -m app.main` is an alternative that reads `HOST`, `PORT` and `DEBUG`. Health check: `curl http://127.0.0.1:8000/health`.
- **Terminal 4.** For a production-style run use `npm run build && npm start`.

Open `http://localhost:3000`.

---

## Using the UI

- **Dashboard (`/`).** Active runs, and completed or ended runs. The **Run status** column is the application's own record, so a paused supervisor still shows `running`.
- **Create a supervisor (`/supervisors/new`).**
  - Name, description, base instruction.
  - The tools it may use and the events that wake it immediately.
  - The minimum, default and maximum wake interval in minutes.
  - The terminal order statuses that end a run, and (under *Advanced*) which order status each event sets.
  - Supervisors are immutable: an existing name creates the next version.
- **Start a run (`/runs/new`).** An order id (one run per order), a supervisor and optional run-specific instructions, one per line. The supervisor list only shows supervisors used by existing runs plus the one you just created (there is no supervisor-list endpoint); you can also paste a supervisor id.
- **The run page (`/runs/{run id}`)**, top to bottom:
  1. Run overview, with the **Run** status and the live **Workflow** state.
  2. Workflow status: state (`reasoning`, `sleeping`, `paused` or `terminal`), last cycle outcome, last wake reason, next scheduled wake, cycle count. Read live from Temporal.
  3. Human controls (active runs only).
  4. Instructions: the base instruction and this run's additional instructions, with a form to add one. Adding one wakes the supervisor; on a paused run it is recorded until Resume.
  5. Inject an event: one of ten event types with optional JSON details.
  6. Observation: memory, timeline (oldest first), actions, tool executions and final output (with whether the LLM or the fallback wrote it).
- **Human controls.**
  - **Pause** keeps the workflow alive but stops reasoning; events are still recorded.
  - **Resume** lets the supervisor re-evaluate.
  - **Interrupt** stops the current cycle: an in-flight LLM decision is discarded, a tool that already started finishes and is recorded.
  - **Terminate** hard-stops the workflow after a confirmation and cannot be undone.
- **Live updates.** An active run's page refreshes every 3 seconds and stops when the run ends. There are no WebSockets.

---

## Live demo simulator

The tools read the mock tables, and events injected in the UI do not change those tables. `backend/scripts/simulate.py` closes that gap for a demo. Each command changes the mock world first and **then** sends the matching event to the running API, so the tools read exactly the state the event describes. It drives the same `ExternalWorld` that the S1 to S5 scenario tests use, against your live stack.

Needs PostgreSQL (`DATABASE_URL` from `backend/.env`) and the API on `http://127.0.0.1:8000` (change with `--api URL`). Start the run in the UI between `place` and `created`.

```bash
python backend/scripts/simulate.py place   DEMO-2001            # mock order row, status created; no event
python backend/scripts/simulate.py created DEMO-2001            # event order_created
python backend/scripts/simulate.py pay     DEMO-2001            # order -> payment_confirmed, event payment_confirmed
python backend/scripts/simulate.py ship    DEMO-2001            # shipment created, order -> shipped, event shipment_created
python backend/scripts/simulate.py delay   DEMO-2001            # shipment and order -> delayed, event shipment_delayed
python backend/scripts/simulate.py message DEMO-2001 "Where is my order?"   # inbound message row, event customer_message_received
python backend/scripts/simulate.py deliver DEMO-2001            # shipment and order -> delivered, event delivered (ends the run)
```

- **Other endings:** `fail-payment` (created to payment_failed) and `cancel` (only before a shipment exists). `delay`, `fail-payment` and `cancel` accept `--reason`.
- **`show ORDER_ID`** prints the run and the mock rows (order and shipment status, `escalated`, customer messages). It is the quickest way to see that a tool changed the world.
- **`reset ORDER_ID --yes`** deletes that order's run and mock rows. It does **not** stop a live workflow; Terminate the run in the UI first.
- **Steps must go in order.** `pay` needs `created`, `ship` needs `payment_confirmed`, `delay` needs `shipped`, and `deliver` needs `shipped` or `delayed`. A step out of order is refused with the reason, and a command that needs a run says so if none exists.
- The UI's **Inject an event** panel is unchanged: it only sends the event.

---

## API overview

FastAPI serves interactive docs at `http://127.0.0.1:8000/docs`. Errors use `{"error": <message>, "code": <CODE>}`. A `202` means the request was accepted at the workflow boundary, not yet processed.

| Method and path | Purpose |
|---|---|
| `GET /health` | health check |
| `POST /api/supervisors` | create a supervisor (same name creates the next version) |
| `GET /api/supervisors/{supervisor_id}` | read a supervisor |
| `POST /api/runs` | create a run and start its workflow (`201`) |
| `GET /api/runs`, `GET /api/runs/{run_id}` | list runs, read a run |
| `POST /api/runs/{run_id}/events` | send an order event as a Signal (`202`) |
| `POST /api/runs/{run_id}/instructions` | add a run-specific instruction (Signal, `202`) |
| `POST /api/runs/{run_id}/pause`, `/resume`, `/interrupt` | human controls (Signals, `202`) |
| `POST /api/runs/{run_id}/terminate` | hard-stop the workflow (`202`) |
| `GET /api/runs/{run_id}/status` | live workflow state (Temporal Query); `409` when closed |
| `GET /api/runs/{run_id}/timeline`, `/memory`, `/actions`, `/tool-executions`, `/final-output` | recorded history from PostgreSQL, oldest first |

- Error codes: `VALIDATION_ERROR` (400), `RUN_NOT_FOUND` (404), `RUN_ALREADY_EXISTS` (409), `RUN_NOT_ACTIVE` (409), `TEMPORAL_UNAVAILABLE` (503).
- The ten event types: `order_created`, `payment_confirmed`, `payment_failed`, `shipment_created`, `shipment_delayed`, `delivered`, `refund_requested`, `customer_message_received`, `no_update_for_n_hours`, `order_cancelled`.
- Nothing generates `no_update_for_n_hours` automatically; the durable scheduled wake-ups already cover the no-update case.

---

## Testing and verification

**Backend.** Needs the migrated PostgreSQL from Setup; it does **not** need a running Temporal server (the tests use Temporal's time-skipping test server, which the SDK may download on first use).

```bash
source .venv/bin/activate
pytest backend/tests -q            # 313 tests
```

- Covers the API, models, workflow, Activities, LLM client and schemas, and the mock tools.
- Five end-to-end scenarios run with a scripted FakeLLM: S1 smooth delivery, S2 delayed shipment, S3 LLM unavailable, S4 human controls, S5 payment failure and cancellation.

**Frontend** (from `frontend/`): `npm run lint`, `npx tsc --noEmit`, `npm run build`. It has no automated test suite of its own; its flows were exercised in a real browser against the real stack with FakeLLM.

**Manual utilities** (not part of pytest, not needed to run the app):

```bash
# Real Temporal dev server + worker + FastAPI + PostgreSQL, scripted FakeLLM (no key, no cost)
python backend/scripts/runtime_validation.py preflight
python backend/scripts/runtime_validation.py all            # runs S1 on real processes, then cleans up
python backend/scripts/runtime_validation.py cleanup        # only if a previous run was interrupted

# Live Gemini check: 2 real requests through the runtime's own LLM client (needs GEMINI_API_KEY)
python backend/scripts/llm_smoke.py

# Real Gemini + real Temporal end to end (about 4 real calls, about 3 minutes, needs GEMINI_API_KEY)
python backend/scripts/gemini_e2e.py

# Production worker + real Temporal against a local OpenAI-compatible stub (no quota)
python backend/scripts/gemini_e2e.py --mock-llm
```

---

## What is real vs mocked

| Component | Status |
|---|---|
| PostgreSQL, FastAPI, Next.js | **Real.** |
| Temporal | **Real.** Validated with a real dev server, worker and separate processes; the scenario tests use the time-skipping test server. |
| Operational tools | **Mocked.** They read and change mock rows in PostgreSQL; nothing external is contacted. |
| FakeLLM | **Test double**, for the tests and runtime validation only. |
| Gemini | **Real provider** (`gemini-3.1-flash-lite`). A full run (real Gemini, production worker, real Temporal, FastAPI, PostgreSQL) produced a final output written by the LLM (`source: llm`). Gemini can return transient 503 or 429 errors; the affected cycle fails cleanly and the next wake recovers. |
| OpenAI | Implemented; offline tests only. |

Not verified with real Gemini: interrupt, terminate, `send_customer_update` and a browser-driven run.

---

## Limitations

- **Proof of concept.** No authentication, no multi-tenancy, no production hardening. The backend has no CORS configuration on purpose.
- **Mocked operations.** The UI does not drive the mock operational state: an order started from the UI has no mock rows until you create them (`simulate.py place`, or Setup step 7), and events injected in the UI do not change them. Use the simulator to change the world and send the event together.
- **LLM dependency.** Without a working key every cycle fails cleanly and the final output comes from the fallback. Only the Gemini path was validated live.
- **Simple wake policy.** A fixed, rule-based list per supervisor. No LLM classifier, no agent-written wake guidance, no special handling of unknown event types.
- **One tool per reasoning cycle**, from a fixed set of four.
- **No `continue_as_new`.** The workflow keeps compact state, but a very long history is not rolled over.
- **Temporal dev server is in-memory.** After a restart PostgreSQL still shows the lost runs as `running`.
- **PostgreSQL and Temporal are not one transaction.** Failures between saving a run and starting its workflow are reported honestly; a run that failed to start keeps its order id.
- **UI.**
  - The supervisor picker only lists supervisors used by existing runs.
  - The page re-renders every 3 seconds, so a half-typed form draft is lost if the backend goes down.
  - The timeline has no filtering or pagination, and only the desktop layout was reviewed.

---

## Troubleshooting

- **`role "postgres" does not exist`.** Set `DATABASE_URL` to use your own user (Setup, step 2).
- **`TEMPORAL_UNAVAILABLE` (503), or "Workflow status unavailable".** Temporal is not running, or the API was started before it. Start Temporal first, then **restart the API**. A run whose workflow could not start is marked `failed` and keeps its order id; start a new run with a different order id.
- **A run stays `running` but nothing happens.** The worker is not running. Start it in terminal 2.
- **The timeline shows "Reasoning failed".**
  - No usable LLM key is configured. Set `LLM_PROVIDER` and `GEMINI_API_KEY` in `backend/.env` and restart the **worker**.
  - Or the provider answered `HTTP 503` or `429` (overload or quota). Nothing is broken: no tool ran and the workflow reasons again at its next wake. To retry sooner, inject an important event.
- **After restarting Temporal, old runs still say `running`.** Their workflows are gone. Start new runs.
- **Tool executions show `failed` with `Order not found` or `Shipment not found`.** The order has no mock rows. Run `python backend/scripts/simulate.py place ORDER_ID` before starting the run (or seed by hand, Setup step 7).
- **Backend tests fail to connect.** Check that PostgreSQL is running, `DATABASE_URL` is correct and `alembic upgrade head` was applied.
- **`npm run dev` creates two agent-rules files in `frontend/`.** The Next.js 16 development server writes them on first start; they are not part of the project and can be deleted.
- **Ports.** Temporal 7233 (gRPC) and 8233 (UI), FastAPI 8000, Next.js 3000.

---

## Demo walkthrough

A path through the product with the orders `DEMO-2001` (full lifecycle) and `DEMO-2002` (terminated). A real Gemini key is needed to see decisions (an ended run with a fallback final output does not count as a real-LLM demo). The LLM chooses the tools, so exact wording varies between runs. The outside world is driven by the [live demo simulator](#live-demo-simulator), and events that need no world change are sent from the UI. The full scene-by-scene recording script is [`DOCS/WALKTHROUGH_SCRIPT.md`](DOCS/WALKTHROUGH_SCRIPT.md).

1. **Configure the LLM.** In the git-ignored `backend/.env` set `LLM_PROVIDER=gemini` and `GEMINI_API_KEY=...` (see [Environment configuration](#environment-configuration)). Start the four processes as in [Running the application](#running-the-application), restarting the **worker** after any `.env` change, and open `http://localhost:3000`.
2. **Create a supervisor** with all four tools; the default important events plus `customer_message_received`; wake interval minimum 1, default 1 and maximum 2 minutes (so a scheduled wake-up happens within the demo; the UI default is 60); `delivered` as a terminal status and the event to status mapping `delivered` to `delivered`.
3. **Place the order, then start the run.** `python backend/scripts/simulate.py place DEMO-2001`, then start a run for `DEMO-2001` in the UI. The first reasoning cycle (workflow start) appears; the supervisor typically calls a read tool such as `get_order_status`, which succeeds against the mock order, and the workflow then sleeps on a durable timer.
4. **Routine events do not wake the supervisor.** In the UI's **Inject an event** panel send `order_created`, then run `simulate.py pay` and `simulate.py ship` for `DEMO-2001`. They are recorded on the timeline and the cycle count does not change.
5. **Scheduled wake-up.** Wait for the timer: a new cycle appears with the wake reason `scheduled_wakeup`.
6. **Add a run instruction** in the UI, for example "If the shipment is delayed, escalate it immediately with priority high. If the customer writes in, reply with a short update using send_customer_update." The supervisor wakes with the reason `instruction_added`.
7. **Important event.** Run `simulate.py delay DEMO-2001`. It wakes the supervisor (`important_event`), which is expected to call `escalate_shipment`. The Tool executions panel shows `success`, and `simulate.py show DEMO-2001` shows `escalated=True`.
8. **Customer message.** In the UI's event panel send `customer_message_received` with `{"message": "Where is my order?"}`. The supervisor is expected to call `send_customer_update`; `simulate.py show DEMO-2001` lists the outbound reply.
9. **Human controls.** Pause (events recorded, no reasoning; inject one by hand in the UI's event panel), Resume (wake reason `resume`), Interrupt.
10. **Finish the run.** `simulate.py deliver DEMO-2001`. The order reaches its terminal status and the run completes. The Final output panel shows the summary, key actions, key learnings and recommendations, and says the **LLM** wrote it (`source: llm`). If it says the fallback wrote it, the LLM call failed; that is not a real-LLM result.
11. **Terminate a second run.** `simulate.py place DEMO-2002`, start a run for it, then use Terminate and confirm. The workflow stops for good and no final output is written.
