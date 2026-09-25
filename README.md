# Order Supervisor

A proof of concept for a long-running AI supervisor that oversees **one order** from creation to completion. Every order gets its own durable [Temporal](https://temporal.io) workflow. Order events reach the workflow as Signals, an LLM decides when to act, when to sleep and when to wake up again, tools carry out the actions, and a final report is written when the order ends.

**Stack:** Next.js (App Router) + Tailwind CSS, FastAPI, Temporal Python SDK, PostgreSQL, and an LLM behind a small client interface (Gemini through its OpenAI-compatible endpoint, or OpenAI).

**Documents:** [Architecture note](ARCHITECTURE.md) · [Assignment](DOCS/PROBLEM_STATEMENT.md) · [Frontend notes](frontend/README.md)

## Contents

1. [How it works](#how-it-works)
2. [Prerequisites](#prerequisites)
3. [Setup](#setup)
4. [Configuration](#configuration)
5. [Running the application](#running-the-application)
6. [Using the UI](#using-the-ui)
7. [Demo walkthrough](#demo-walkthrough)
8. [API overview](#api-overview)
9. [Testing](#testing)
10. [What is real vs mocked](#what-is-real-vs-mocked)
11. [Limitations](#limitations)
12. [Troubleshooting](#troubleshooting)
13. [Project structure](#project-structure)

---

## How it works

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

- The browser only talks to Next.js. Server Components read from FastAPI and Server Actions write to it.
- **FastAPI** creates supervisors and runs in PostgreSQL, starts the workflow once (in `POST /api/runs`), and afterwards only signals, queries or terminates it.
- **Temporal** hosts one deterministic `OrderWorkflow` per order (id `order-{order_id}`). It owns durable timers, Signals and compact workflow state.
- **Activities** do all the non-deterministic work: persisting to PostgreSQL, calling the LLM and running tools.
- **The LLM** only returns a structured JSON decision. The workflow validates it and runs at most one tool per reasoning cycle.
- **Tools** are four mocks that read and change mock order, shipment and message rows in PostgreSQL: `get_order_status`, `get_shipment_status` (read-only), `escalate_shipment` and `send_customer_update` (side effects).

The supervisor reasons on three triggers (workflow start, an important event, a scheduled wake-up) and also when a run instruction is added or a paused run is resumed. Between wakes it sleeps on a durable timer. A run ends when the order reaches a terminal status; the workflow then writes a final summary, key actions, learnings and recommendations. Diagrams and the reasoning behind the design are in the [architecture note](ARCHITECTURE.md).

---

## Prerequisites

| Requirement | Version |
|---|---|
| Python | 3.9 (verified with 3.9.6; newer versions were not tried) |
| Node.js and npm | Node 20.9 or newer (verified with Node 24) |
| PostgreSQL | 13 or newer (verified with 17); the seed SQL uses `gen_random_uuid()` |
| Temporal dev server | The Temporal CLI (`temporal server start-dev`), or the SDK-managed alternative in [Running the application](#running-the-application) |
| LLM API key | Optional. Needed only to see real reasoning; see [Configuration](#configuration) |

---

## Setup

Run all commands from the repository root unless stated.

**1. Python environment and backend dependencies**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
```

**2. PostgreSQL database**

```bash
createdb order_supervisor
```

The default `DATABASE_URL` uses the role `postgres` with password `postgres`. On a Homebrew PostgreSQL install there is usually no `postgres` role; use your own user instead, for example `postgresql+asyncpg://YOUR_OS_USER@localhost:5432/order_supervisor`.

**3. Configuration**

```bash
cp backend/.env.example backend/.env
```

Edit `backend/.env` (see [Configuration](#configuration)). This file is git-ignored; real values, including API keys, belong only there.

**4. Database migrations**

```bash
cd backend && alembic upgrade head && cd ..
```

This creates the 11 application tables. `alembic current` should report `821bc1bc7abf (head)`.

**5. Frontend dependencies**

```bash
cd frontend && npm install && cd ..
```

Optionally `cp frontend/.env.example frontend/.env.local` if the backend is not at `http://127.0.0.1:8000`.

**6. Mock operational data (needed for successful tool results)**

The tools act on the tables `mock_orders`, `mock_shipments` and `mock_customer_messages`. Only the simulator and the test factories create rows there; **starting a run from the UI does not**. For an order with no mock row, every tool call is recorded as a failed tool execution (`Order not found`). To see tools succeed, seed the order **before** starting its run, using the same order id:

```bash
psql order_supervisor <<'SQL'
INSERT INTO mock_orders (id, order_id, status, customer_id)
VALUES (gen_random_uuid(), 'DEMO-1001', 'shipped', 'CUSTOMER-1001');
INSERT INTO mock_shipments (id, order_id, shipment_id, status, tracking_number)
VALUES (gen_random_uuid(), 'DEMO-1001', 'SHIP-1001', 'in_transit', 'TRACK-1001');
SQL
```

Adjust the `psql` connection to match your `DATABASE_URL`. Two things to know:

- Events injected in the UI are Signals to the workflow only. **They do not change the mock tables**; a tool reads whatever the rows say. To make the world move, update the rows yourself, for example `UPDATE mock_shipments SET status = 'delayed', delay_reason = 'Carrier capacity shortage' WHERE order_id = 'DEMO-1001';`
- The order status shown on the run page comes from the supervisor's own event-to-status mapping, which is separate from `mock_orders.status`.

---

## Configuration

The backend reads environment variables and `.env` from the working directory or `backend/.env`.

| Variable | Default | Meaning |
|---|---|---|
| `APP_ENV` | `development` | `development`, `staging`, `production` or `test` |
| `DEBUG` | `false` | Debug mode; auto-reload only with `python -m app.main` |
| `HOST`, `PORT` | `127.0.0.1`, `8000` | Bind address used by `python -m app.main` |
| `DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@localhost:5432/order_supervisor` | Async PostgreSQL URL (asyncpg driver) |
| `TEMPORAL_ADDRESS` | `localhost:7233` | Temporal server address (API and worker) |
| `TEMPORAL_NAMESPACE` | `default` | Temporal namespace |
| `LLM_PROVIDER` | `openai` in code; `gemini` in `.env.example` | `gemini` or `openai` |
| `GEMINI_API_KEY` | none | Key for `LLM_PROVIDER=gemini` |
| `LLM_MODEL` | Gemini: `gemini-3.1-flash-lite`; OpenAI: required | Model name |
| `LLM_BASE_URL` | Gemini's OpenAI-compatible endpoint | Override the provider URL |
| `LLM_REASONING_EFFORT` | not sent | Sent to the provider only when set. Some models reject it. |
| `LLM_TIMEOUT_SECONDS` | `45` | Per-request LLM timeout (kept below the 60 s reasoning Activity timeout) |
| `LLM_API_KEY` | none | Key for `LLM_PROVIDER=openai` (together with `LLM_MODEL`) |

Frontend (`frontend/.env.local`, optional): `API_BASE_URL`, default `http://127.0.0.1:8000`. It is read on the Next.js server only.

### Which LLM configuration to use

| Situation | Configuration |
|---|---|
| **No key** | Leave the key empty. The app, UI, events, instructions and controls all work, but every reasoning cycle fails cleanly: the timeline records "Reasoning failed after retries", no tool runs, and the final output comes from a deterministic fallback. |
| **Gemini** | `LLM_PROVIDER=gemini` and `GEMINI_API_KEY=<key>` in `backend/.env`. The configuration validated end to end is `LLM_MODEL=gemini-3.6-flash` with `LLM_REASONING_EFFORT=low`; set both explicitly. Other Gemini models may reject `reasoning_effort`, so leave it empty for them. |
| **OpenAI** | `LLM_PROVIDER=openai`, `LLM_API_KEY` and `LLM_MODEL` (Responses API). Implemented and covered by offline tests only. |
| **FakeLLM** | A scripted test double, injected only by the test suite and `runtime_validation.py`. Not selectable through configuration. |

Restart the **worker** after changing `backend/.env`. Never commit a real key.

---

## Running the application

Start four processes in four terminals, **in this order**: the API connects to Temporal once, at startup. In each backend terminal run `source .venv/bin/activate` first.

| Terminal | What | Command |
|---|---|---|
| 1 | Temporal dev server (gRPC `localhost:7233`, web UI `http://localhost:8233`) | `temporal server start-dev` |
| 2 | Worker: hosts `OrderWorkflow` and its Activities | `cd backend && python -m app.temporal.worker` |
| 3 | FastAPI on `http://127.0.0.1:8000` (docs at `/docs`) | `cd backend && uvicorn app.main:app --port 8000` |
| 4 | Next.js on `http://localhost:3000` | `cd frontend && npm run dev` |

- The Temporal dev server keeps its data **in memory**: restarting it forgets every running workflow.
- If you do not have the Temporal CLI, `python backend/scripts/runtime_validation.py server` starts an SDK-managed dev server instead (it downloads a binary on first use).
- The worker prints nothing when it starts. Check the API with `curl http://127.0.0.1:8000/health`.

Open `http://localhost:3000`.

---

## Using the UI

- **Dashboard (`/`).** Active runs, and completed or ended runs. The **Run status** column is the application's own record, so a paused supervisor still shows `running`.
- **Create a supervisor (`/supervisors/new`).** Name, description, base instruction, the tools it may use, the events that wake it immediately, the minimum, default and maximum wake interval in minutes, the terminal order statuses that end a run and (under *Advanced*) which order status each event sets. Supervisors are immutable: an existing name creates the next version.
- **Start a run (`/runs/new`).** An order id (one run per order), a supervisor and optional run-specific instructions, one per line.
- **The run page (`/runs/{id}`)** shows, top to bottom:
  1. Run overview, with the run status and the live workflow state.
  2. Workflow status: state (`reasoning`, `sleeping`, `paused` or `terminal`), last cycle outcome, last wake reason, **next scheduled wake**, cycle count. Read live from Temporal.
  3. Human controls (active runs only).
  4. Instructions: the supervisor's base instruction, this run's additional instructions and a form to add one.
  5. Inject an event: one of ten event types with an optional JSON payload.
  6. Observation: memory, timeline, actions, tool executions and the final output.
- **Human controls.** *Pause* stops reasoning but keeps the workflow alive (events are still recorded). *Resume* wakes the supervisor. *Interrupt* drops the current cycle. *Terminate* hard-stops the workflow after a confirmation and gives no final output.
- **Live updates.** An active run's page refreshes itself every 3 seconds. There are no WebSockets.

---

## Demo walkthrough

A path through the product with the order `DEMO-1001`. A real Gemini key is needed to see decisions. The LLM chooses the tools, so the exact wording varies between runs.

1. **Configure the LLM** in `backend/.env` (see [Configuration](#configuration)), start the four processes and open `http://localhost:3000`. Restart the worker after changing `.env`.
2. **Seed the mock world** with the SQL in [Setup](#setup), step 6.
3. **Create a supervisor** with all four tools, `shipment_delayed` and `customer_message_received` as important events, `delivered` as a terminal status and the event-to-status mapping `delivered` to `delivered`.
4. **Start a run** for `DEMO-1001`. The first reasoning cycle appears on the run page; the supervisor typically calls a read tool such as `get_order_status`, then the workflow sleeps on a durable timer (see *next scheduled wake*).
5. **Inject routine events** (`order_created`, `payment_confirmed`, `shipment_created`). They are recorded without waking the supervisor.
6. **Add a run instruction**, for example "If the shipment is delayed, escalate it immediately with priority high." The supervisor wakes with the reason `instruction_added`.
7. **Move the mock world, then send the important event.** Run `UPDATE mock_shipments SET status = 'delayed', delay_reason = 'Carrier capacity shortage' WHERE order_id = 'DEMO-1001';` and inject `shipment_delayed`. It wakes the supervisor (`important_event`), which is expected to call `escalate_shipment`. The Tool executions panel shows `success` and `mock_shipments.escalated` becomes true.
8. **Try the human controls:** Pause (events recorded, no reasoning), Resume, Interrupt.
9. **Finish the run.** Update the mock rows to delivered (`UPDATE mock_shipments SET status = 'delivered' ...; UPDATE mock_orders SET status = 'delivered' ...;` for `DEMO-1001`), then inject `delivered`. The order reaches its terminal status and the run completes.
10. **Check the final output.** The Final output panel shows the summary, key actions, key learnings and recommendations, and says the **LLM** wrote it (`source: llm`). If it says the fallback wrote it, the LLM call failed.
11. **Terminate a second run.** Start another order and press Terminate. The workflow stops for good and no final output is written.

---

## API overview

FastAPI serves interactive docs at `http://127.0.0.1:8000/docs`. Errors use `{"error": <message>, "code": <CODE>}`. A `202` means the request was accepted at the workflow boundary, not yet processed.

| Method and path | Purpose |
|---|---|
| `GET /health` | Health check |
| `POST /api/supervisors` | Create a supervisor (same name creates the next version) |
| `GET /api/supervisors/{id}` | Read a supervisor |
| `POST /api/runs` | Create a run and start its workflow (`201`) |
| `GET /api/runs`, `GET /api/runs/{id}` | List runs, read a run |
| `POST /api/runs/{id}/events` | Send an order event into the workflow as a Signal (`202`) |
| `POST /api/runs/{id}/instructions` | Add a run-specific instruction (`202`) |
| `POST /api/runs/{id}/pause`, `/resume`, `/interrupt` | Human controls (Signals, `202`) |
| `POST /api/runs/{id}/terminate` | Hard-stop the workflow (`202`) |
| `GET /api/runs/{id}/status` | Live workflow state (Temporal Query); `409` when the workflow is closed |
| `GET /api/runs/{id}/timeline`, `/memory`, `/actions`, `/tool-executions`, `/final-output` | Recorded history from PostgreSQL |

Common error codes: `VALIDATION_ERROR` (400), `RUN_NOT_FOUND` (404), `RUN_ALREADY_EXISTS` (409), `RUN_NOT_ACTIVE` (409), `TEMPORAL_UNAVAILABLE` (503).

The ten event types are `order_created`, `payment_confirmed`, `payment_failed`, `shipment_created`, `shipment_delayed`, `delivered`, `refund_requested`, `customer_message_received`, `no_update_for_n_hours` and `order_cancelled`. Nothing generates `no_update_for_n_hours` automatically; the durable scheduled wake-ups already cover the no-update case.

---

## Testing

Backend (needs the migrated PostgreSQL from Setup; it does **not** need a running Temporal server, because the tests use Temporal's time-skipping test server):

```bash
source .venv/bin/activate
pytest backend/tests -q      # 313 tests
```

The suite covers the API, models, workflow, Activities, LLM client and schemas, the mock tools and five end-to-end scenarios run against Temporal's time-skipping server with a scripted FakeLLM: S1 smooth delivery, S2 delayed shipment, S3 LLM unavailable, S4 human controls, S5 payment failure and cancellation.

Frontend (from `frontend/`): `npm run lint`, `npx tsc --noEmit`, `npm run build`. The frontend has no automated tests of its own.

Manual validation utilities (not part of pytest, not needed to run the app):

```bash
python backend/scripts/runtime_validation.py all     # real Temporal + worker + API + PostgreSQL, scripted FakeLLM, no key
python backend/scripts/llm_smoke.py                  # 2 real LLM requests (needs GEMINI_API_KEY)
python backend/scripts/gemini_e2e.py                 # real Gemini + real Temporal end to end (about 4 calls, about 3 minutes)
python backend/scripts/gemini_e2e.py --mock-llm      # production worker against a local stub, no quota
```

---

## What is real vs mocked

| Component | Status |
|---|---|
| PostgreSQL, FastAPI, Next.js | Real |
| Temporal | Real. Validated with a real dev server and worker; the scenario tests use the time-skipping test server. |
| Operational tools | **Mocked.** They read and change mock rows in PostgreSQL; nothing external is contacted. |
| FakeLLM | Test double for tests and runtime validation only |
| Gemini | Real provider. A full run (real Gemini, production worker, real Temporal, FastAPI, PostgreSQL) passed with `gemini-3.6-flash` and produced a final output written by the LLM. Gemini can return transient 503 or 429 errors; the affected cycle fails cleanly and the next wake recovers. |
| OpenAI | Implemented; offline tests only |

Not verified with real Gemini: interrupt, terminate, `send_customer_update` and a browser-driven run (the browser flows were exercised with FakeLLM).

---

## Limitations

- **Proof of concept.** No authentication, no multi-tenancy, no production hardening. The backend has no CORS configuration on purpose.
- **Mocked operations.** An order started from the UI has no mock rows until you seed them, and injected events do not change them.
- **LLM dependency.** Without a working key every cycle fails cleanly and the final output comes from the fallback.
- **Simple wake policy.** A fixed, rule-based list of important event types per supervisor. No LLM classifier, no agent-written wake guidance, no handling of unknown event types.
- **One tool per reasoning cycle**, from a fixed set of four.
- **No `continue_as_new`.** The workflow keeps compact state, but a very long history is not rolled over.
- **Temporal dev server is in-memory.** After a restart PostgreSQL still shows lost runs as `running`.
- **PostgreSQL and Temporal are not one transaction.** Failures between saving a run and starting its workflow are reported honestly; a run that failed to start keeps its order id.
- **A persistence outage of about 30 seconds or more fails the workflow.**
- **UI.** The supervisor picker lists only supervisors used by existing runs (there is no supervisor-list endpoint). The page re-renders every 3 seconds, so a half-typed form draft is lost if the backend goes down. The timeline has no filtering or pagination.

---

## Troubleshooting

- **`role "postgres" does not exist`.** Set `DATABASE_URL` to use your own user (see Setup, step 2).
- **`TEMPORAL_UNAVAILABLE` (503) or "Workflow status unavailable".** Temporal was not running when the API started. Start Temporal first, then **restart the API**. A run whose workflow could not start is marked `failed` and keeps its order id; start a new run with a different order id.
- **A run stays `running` but nothing happens.** The worker is not running. Start it in terminal 2.
- **The timeline shows "Reasoning failed".** No usable LLM key is configured, or the provider returned an error (`HTTP 503` or `429` means overload or quota). Nothing is broken: no tool ran and the workflow reasons again at its next wake. Set `LLM_PROVIDER` and `GEMINI_API_KEY`, then restart the worker; to retry sooner, inject an important event.
- **After restarting Temporal, old runs still say `running`.** Their workflows are gone. Start new runs.
- **Tool executions fail with `Order not found` or `Shipment not found`.** The order has no mock rows. Seed them before starting the run (Setup, step 6).
- **Backend tests fail to connect.** Check that PostgreSQL is running, `DATABASE_URL` is correct and `alembic upgrade head` has been applied.
- **`npm run dev` creates `frontend/AGENTS.md` and `frontend/CLAUDE.md`.** Next.js writes them on first start; they are not part of the project and can be deleted.
- **Ports.** Temporal 7233 (gRPC) and 8233 (UI), FastAPI 8000, Next.js 3000.

---

## Project structure

```
.
├── README.md
├── ARCHITECTURE.md              short architecture note
├── .env.example                 configuration template (same as backend/.env.example)
├── DOCS/                        assignment, implementation rules, specification
├── backend/
│   ├── requirements.txt
│   ├── migrations/versions/     3 Alembic migrations
│   ├── scripts/                 runtime_validation.py, llm_smoke.py, gemini_e2e.py
│   ├── app/
│   │   ├── main.py, config.py   FastAPI app factory, settings
│   │   ├── api/                 supervisors, runs (events, instructions, controls), observation, health
│   │   ├── db/                  SQLAlchemy models, session, mock operational models
│   │   ├── temporal/            workflows.py, worker.py, activities/, decision_rules.py,
│   │   │                        wake_policy.py, supervisor_config.py, contracts.py, types.py
│   │   ├── llm/                 client (Gemini / OpenAI), prompts, output schemas, FakeLLM
│   │   ├── tools/               tool registry and the database-backed mock tools
│   │   └── simulation/          external-world simulator and scenarios S1 to S5 (used by tests)
│   └── tests/                   pytest suite (313 tests)
└── frontend/
    └── src/
        ├── app/                 pages: dashboard, supervisors, runs (run page, Server Actions, controls)
        ├── api/                 the only code that talks HTTP to the backend
        └── components/          shared UI and the LiveRefresh polling component
```
