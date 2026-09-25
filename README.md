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
| **Temporal** | Durable orchestration: the workflow's lifecycle, its Signals, its timers and its compact workflow state. |
| **`OrderWorkflow`** | Decides *when* the supervisor reasons and *what is allowed to happen*. It stays **deterministic**: no I/O, no randomness, time only through Temporal. |
| **Activities** | All non-deterministic or external work: LLM calls, PostgreSQL writes and tool execution. |
| **PostgreSQL** | Application and history data: supervisors, runs, events, timeline, actions, tool executions, memory snapshots, final output, plus the mock operational tables the tools act on. |
| **LLM** | Structured reasoning: it returns a JSON decision. It has **no side effects**; the workflow validates the decision and runs any tool itself, through an Activity. |
| **Tools** | Four mocked operational tools that read and change mock order, shipment and message rows in PostgreSQL: two read-oriented (`get_order_status`, `get_shipment_status`) and two with side effects (`escalate_shipment`, `send_customer_update`). The next wake time is part of the LLM's structured decision and a run closes when the order reaches a terminal status, so there is no separate schedule or close tool. Those rows are created by the simulator and the tests, or seeded by hand (see [Setup](#setup)); the API and the UI do not create them. |
| **FastAPI** | A thin boundary over PostgreSQL and the Temporal client. |
| **Next.js** | The UI. The browser only ever talks to Next.js; Server Components read from FastAPI and Server Actions write to it. |

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

- The browser only ever talks to Next.js (Server Components read, Server Actions write). It never talks to FastAPI, Temporal or PostgreSQL directly.
- **FastAPI** is a thin boundary. It creates supervisors and runs in PostgreSQL, **starts** the workflow (once, in `POST /api/runs`), and afterwards only **signals, queries or terminates** an existing workflow. It reads the recorded history straight from PostgreSQL (dotted line) and never writes events itself: the workflow's Activities do.
- **Temporal is the orchestration layer**: durable timers, Signals and workflow state. The worker process (`python -m app.temporal.worker`) hosts the deterministic `OrderWorkflow` (id `order-{order_id}`) and runs its Activities.
- **Activities do all the non-deterministic work**: persisting to PostgreSQL, calling the LLM (which only returns a structured decision) and running the tools. The mock tools read and change the `mock_*` tables in PostgreSQL. PostgreSQL stores application and history data; it is not the workflow engine.

### Order workflow lifecycle

How one workflow progresses. One `OrderWorkflow` per order; the loop never spins: it either reasons once or waits on a durable timer.

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

Colours: blue = Signals, green = LLM reasoning, orange = a tool, grey = waiting, purple = paused, amber = a stale decision discarded, teal = terminal handling, red = hard stop, yellow cylinders = the run's status row in PostgreSQL (an application record written around the workflow, not part of the workflow engine).

- **Two lifecycles, side by side.** The Temporal workflow (indigo path) and the run's status row in PostgreSQL (yellow cylinders) are separate. The API writes `starting` before it contacts Temporal, then `running` once `start_workflow` succeeds, or `failed` if it does not. The workflow's `complete_run` Activity writes `completed`; the API writes `terminated` after a hard stop. Pause is workflow state only: the row stays `running`. If the workflow itself fails, nothing updates the row (a documented limitation).
- **Pause is a branch, not an end.** A paused workflow does no reasoning and its timer wake is suspended, but events are still recorded; Resume returns it to the Reason step. A terminal status still ends a paused workflow.
- **Terminal handling, in order.** Terminal status reached, flush the records Signal handlers queued, generate the final output (LLM, or the deterministic fallback), flush anything that arrived meanwhile, run `complete_run` (saves the final output and marks the run `completed`), drain any Signals that arrived during `complete_run`, and end. Reasoning never resumes, and late Signals are persisted but cannot reopen the workflow.
- **How a workflow starts, and how Signals reach it.** The API starts a workflow in exactly one place: `POST /api/runs` calls `start_workflow` (the run row is saved as `starting` first and set to `running`, or `failed`, afterwards). Events, instructions, Pause, Resume and Interrupt are ordinary Signals sent to a workflow that is already running; if the run is not active, or Temporal reports the workflow closed or missing, the API answers `409 RUN_NOT_ACTIVE`. **No incoming event can start a workflow, and the application does not use Temporal's Signal-With-Start.** The workflow initializes its state in `@workflow.init`, before any Signal handler can run, so a Signal that arrives in the very first activation still sees the configuration; a regression test proves that exact situation using Signal-With-Start, and an important event delivered that early is consumed by the workflow-start reasoning pass instead of causing a second cycle.
- **The three triggers** are the assignment's: workflow start, an important incoming event, a scheduled wake-up. A run instruction and Resume also wake the supervisor (not while it is paused or the order is terminal). Signal handlers only update state and queue records; the main loop persists them.
- **Terminal is not an LLM decision.** A run ends when the order's status reaches one of the supervisor's configured *terminal order statuses*, which happens when an event arrives whose type the supervisor maps to a status (for example `delivered` to `delivered`). No further reasoning cycle runs. If the order becomes terminal while a decision is in flight, that decision is **discarded** (recorded as `discarded_terminal`) and the workflow goes straight to the final output. The final output is written by the LLM, with a deterministic fallback if that fails.
- **A cycle runs at most one tool.** The workflow executes it through an Activity, never the LLM. Sleeping is a real Temporal timer; a routine event does not move the scheduled wake-up.
- **Human controls.** Pause stops reasoning (events are still recorded), Resume wakes the supervisor, Interrupt drops the in-flight cycle, and Terminate is Temporal's client-side hard stop (not a Signal); the API then records the run as `terminated`.

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

- **Trigger 2 is the "important event" case.** Every event is recorded and can update the order status; the wake policy then checks whether the event type is in the supervisor's `important_event_types` (and the supervisor is not paused, the order is not terminal, and no wake is already queued). A routine event leaves the timer untouched and is seen at the next wake-up. A run instruction and Resume are separate, extra triggers that reach the same Reasoning Activity (dotted); an instruction added while the supervisor is paused is only recorded, and Resume is what ends the pause and wakes it.
- **The LLM only returns a decision, and has no side effects.** Its input is compact memory, new and recent events, the supervisor and run instructions and the enabled tools; it never sees the full history. The Activity checks the JSON against the strict schema and retries a bad answer; the decision holds an assessment, an optional tool with its input, the next wake in minutes, and the memory update (a situation summary and open concerns).
- **The workflow validates before acting.** It checks the decision against three things that may have changed while the LLM was thinking: the cycle was **interrupted**, the supervisor was **paused**, or the order became **terminal**. Any of these makes the decision stale. Otherwise it re-checks the tool against the fixed registry and the supervisor's enabled tools (one tool at most, with its required inputs) and clamps the requested wake time to the supervisor's minimum and maximum. A tool request that fails this check is dropped and noted on the timeline, and the memory update and next wake still apply.
- **"Nothing applied" is precise.** A stale decision or a failed LLM call runs no tool, saves no memory snapshot and leaves the pending events pending; the workflow records only the cycle outcome and a system note on the timeline, and schedules its next wake (or stays paused). **A failed or interrupted cycle never ends the workflow**: it stays alive and reasons again at its next wake (the exception is a decision made stale by a terminal status, which proceeds to terminal handling). The LLM never mutates workflow state itself.
- **Terminal is not a decision.** When the order status becomes terminal the workflow stops reasoning and produces the final output (see the lifecycle diagram).

### Representative demo flow

This is the sealed **S2 "delayed shipment"** scenario, checked step by step against its test (`backend/tests/test_scenario_s2_delayed_shipment.py`). It is a representative flow and **not a hard-coded path**: the events come from the simulator or an operator, and in a live run the LLM chooses the tools. In the S2 test the simulator first changes the mock tables (order, shipment, delay, customer message) and then sends each event; the UI's event panel only sends the event (see [Setup](#setup), step 7, to prepare the mock rows for a UI-driven run). Every event and instruction reaches the workflow as a Signal through FastAPI. The supervisor used here lists `shipment_delayed` and `customer_message_received` as important events. The run has six events, five reasoning cycles and one final-output call.

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
- **Why Gemini is optional.** The application runs without a key (reasoning then fails cleanly, see [Limitations](#limitations)). Gemini is the real provider that was validated with a live smoke test and a full real run through Temporal; it is reached through Google's OpenAI-compatible endpoint with the `openai` SDK.
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

The tools act on the mock tables `mock_orders`, `mock_shipments` and `mock_customer_messages`. Only the simulator and the test factories create those rows; **starting a run from the UI does not**. For an order that has no mock row, every tool call is recorded as a **failed** tool execution with the error `Order not found` (the supervisor still runs and can reason about the failure). To see tools succeed, the order needs mock rows **before** its run starts, with the same order id. Two ways:

- **Recommended: the [live demo simulator](#live-demo-simulator).** `python backend/scripts/simulate.py place DEMO-1001` creates the mock order. The later simulator commands change the mock world and send the matching event together.
- **Manual alternative:** seed an order by hand:

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
| `LLM_MODEL` | `gemini-3.1-flash-lite` for Gemini; required for OpenAI | model name |
| `LLM_BASE_URL` | Gemini's OpenAI-compatible endpoint for Gemini | override the provider URL |
| `LLM_REASONING_EFFORT` | not sent | reasoning effort, sent to the provider only when set (some models reject it) |
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
| **Gemini** (real reasoning) | `LLM_PROVIDER=gemini` and `GEMINI_API_KEY=<your key>` in `backend/.env`. The model and base URL default to `gemini-3.1-flash-lite` and Google's OpenAI-compatible endpoint; reasoning effort is not sent unless `LLM_REASONING_EFFORT` is set. |
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

The ten event types are `order_created`, `payment_confirmed`, `payment_failed`, `shipment_created`, `shipment_delayed`, `delivered`, `refund_requested`, `customer_message_received`, `no_update_for_n_hours` and `order_cancelled`. All are accepted through the API and the UI; nothing generates `no_update_for_n_hours` automatically (the durable scheduled wake-ups already cover the no-update case).

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

# Real Gemini + real Temporal end to end (about 4 real Gemini calls, about 3 minutes, needs GEMINI_API_KEY)
python backend/scripts/gemini_e2e.py

# Production worker + real Temporal, driven against a local OpenAI-compatible stub (no quota)
python backend/scripts/gemini_e2e.py --mock-llm
```

What has been verified: the backend suite (313 passing); the real Temporal runtime validation of S1 with the scripted FakeLLM; the live Gemini smoke test (reasoning decision and final output both valid); a `--mock-llm` rehearsal of the full pipeline with the production worker; and the real Gemini + Temporal end-to-end run (see the table below). The frontend flows (creation, event injection, instructions, human controls, observation and polling) were exercised in a real browser against the real stack with FakeLLM. The frontend has no automated test suite of its own.

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
| Gemini | **Real provider, smoke-tested.** A live smoke test passed (2 real calls: a reasoning decision and a final output, both valid). A later re-run of the smoke script returned HTTP 503 and then 429 (provider load and quota) and did not pass; the script has no retries, unlike the workflow. |
| Full Gemini + Temporal end to end | **Verified.** Run on 2026-09-25 with `backend/scripts/gemini_e2e.py` against the real Gemini API (model `gemini-3.6-flash`, the production worker, a real Temporal dev server, FastAPI and PostgreSQL, no injected LLM): a real decision on workflow start, an important event and a durable timer, real tool Activities that changed the mock world, and a final output written by Gemini (`source: llm`, not the fallback). The first attempt hit transient Gemini HTTP 503 responses: one reasoning cycle failed cleanly after its retries and the next scheduled wake recovered, so the script's strict clean-run check failed; a second attempt passed with no hard failures. A further real run that used the README's own seed SQL and added an instruction to the live run also passed (its driver is kept outside the repository). The `--mock-llm` rehearsal remains available for a no-quota check of the pipeline and the HTTP request shape. |
| OpenAI provider | Implemented; covered by offline tests only. |

---

## Limitations

- **Proof of concept.** No authentication, no multi-tenancy and no production hardening. The backend has no CORS configuration on purpose: the browser talks only to Next.js.
- **Mocked operations.** No real commerce, shipping or messaging integrations. The UI does not drive the mock operational state: an order started from the UI has no mock rows until you create them (`simulate.py place`, or Setup step 7), and events injected in the UI do not change them. Use the simulator to change the world and send the event together; tools on an unseeded order fail with `Order not found`.
- **LLM dependency.** Real supervisor decisions need a provider key. Without one every reasoning cycle fails cleanly and the final output comes from the deterministic fallback. Only the Gemini path has been validated against a live provider, and Gemini can answer with transient 503 or 429 errors: the affected reasoning cycle then fails cleanly ("Reasoning failed after retries; no tool executed") and the supervisor tries again at its next wake.
- **Simple wake policy.** Whether an event wakes the supervisor is a fixed, rule-based list per supervisor. There is no LLM classifier, no agent-written wake guidance and no special handling of unknown events (event types are a fixed, validated vocabulary).
- **One tool per reasoning cycle**, from a fixed set of four.
- **Long histories.** The workflow does not use `continue_as_new`; it keeps only compact state (for example the 20 most recent events) but a very long-running workflow's history is not rolled over.
- **Temporal dev server is in-memory.** Restarting it loses running workflows, while PostgreSQL still shows their runs as `running`. Use a Temporal server with persistence for anything beyond a demo.
- **PostgreSQL and Temporal are not one transaction.** Creating a run saves the row, then starts the workflow, then records the outcome; failures between the steps are reported honestly (a run that failed to start keeps its order id).
- **UI scope.** The supervisor picker only lists supervisors used by existing runs (the backend has no supervisor-list endpoint). Live updates re-render the whole page every 3 seconds; if the backend goes down the page shows an error panel and a half-typed form draft is lost. The timeline is oldest first with no filtering or pagination. Only the desktop layout was reviewed.
- **Verification scope.** The frontend has no automated tests and was exercised against the real stack with FakeLLM, not with a real LLM in the browser. The real Gemini + Temporal end to end was run through the API and scripts (see the table above). The OpenAI provider has not been validated live.

---

## Troubleshooting

- **`role "postgres" does not exist`.** Your PostgreSQL has no `postgres` role. Set `DATABASE_URL` to use your own user, for example `postgresql+asyncpg://YOUR_OS_USER@localhost:5432/order_supervisor`.
- **Creating a run says `TEMPORAL_UNAVAILABLE` (503), or the run page says "Workflow status unavailable".** Temporal is not running, or the API was started before it. The API connects to Temporal only once, when it starts, and never retries: start Temporal first, then **restart the API**. A run whose workflow could not start is marked `failed` and keeps its order id, so start a new run with a different order id.
- **A run stays `running` but nothing happens.** The worker is not running. Start it in terminal 2; Temporal delivers the queued work when it connects.
- **Runs exist but show no supervisor decisions; the timeline shows "Reasoning failed".** No usable LLM key is configured (see [Which LLM configuration to use](#which-llm-configuration-to-use)). Set `LLM_PROVIDER=gemini` and `GEMINI_API_KEY` in `backend/.env` and restart the **worker**.
- **After restarting Temporal, old runs still say `running`.** The dev server keeps state in memory only, so those workflows are gone. Start new runs.
- **The timeline shows "Reasoning failed after retries ... HTTP 503" or "HTTP 429".** Gemini was overloaded or rate-limited. Nothing is broken: no tool ran, the workflow stays alive and reasons again at its next scheduled wake (or on the next important event). Wait a minute and, if needed, inject an important event to trigger another cycle.
- **Tool executions show `failed` with `Order not found` (or `Shipment not found`).** The order has no mock operational rows. Run `python backend/scripts/simulate.py place ORDER_ID` before starting the run (or seed them by hand, Setup step 7). A shipment row appears after `simulate.py ship`.
- **Backend tests fail to connect.** Check that PostgreSQL is running, `DATABASE_URL` is correct and `alembic upgrade head` has been applied.
- **`npm run dev` creates `frontend/AGENTS.md` and `frontend/CLAUDE.md`.** The Next.js 16 development server writes these two files on first start. They are not part of the project and can be deleted.
- **Ports.** Temporal uses 7233 (gRPC) and 8233 (UI), FastAPI 8000 and Next.js 3000.

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
