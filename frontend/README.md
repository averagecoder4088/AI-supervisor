# Order Supervisor: frontend

Next.js (App Router) + TypeScript + Tailwind CSS. A simple dashboard over the existing FastAPI backend.
It can create supervisors, start runs and inject events and add run-specific instructions to an active run; run controls come later.

## Run it

```bash
# 1. Backend (from the project root; needs PostgreSQL. A Temporal server is only needed to create runs)
cd backend && ../.venv/bin/uvicorn app.main:app --port 8000

# 2. Frontend
cd frontend
npm install
npm run dev            # http://localhost:3000
```

Production build: `npm run build && npm start`. Checks: `npm run lint`.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `API_BASE_URL` | `http://127.0.0.1:8000` | Base URL of the FastAPI backend. Server-side only. |

Copy `.env.example` to `.env.local` to override. All backend calls run on the server (Server Components), so the
browser never contacts the backend directly and no CORS setup is needed yet.

## Routes

| Route | Shows |
|---|---|
| `/` | Dashboard: active and completed runs |
| `/supervisors` | Supervisors referenced by existing runs (read-only) |
| `/runs/[runId]` | Run observation: overview, live **Workflow status**, **Memory**, **Timeline**, **Actions**, **Tool executions** and **Final output** (each loads on its own, so one failing source does not break the page; a **Refresh** button re-reads them, there is no polling). For an active run also **Human controls** (pause, resume, interrupt, terminate), an **Additional instructions** form and an **Inject an event** form |
| `/supervisors/new` | Create a supervisor (tools, wake behaviour, terminal statuses, status mapping) |
| `/runs/new` | Start a run: order ID, supervisor, run-specific instructions |

## Backend endpoints used

`GET /api/runs`, `GET /api/runs/{run_id}`, `GET /api/supervisors/{supervisor_id}`, `POST /api/supervisors`,
`POST /api/runs`, `POST /api/runs/{run_id}/events`, `POST /api/runs/{run_id}/instructions`,
`GET /api/runs/{run_id}/status`, `GET /api/runs/{run_id}/timeline|memory|actions|tool-executions|final-output`,
`POST /api/runs/{run_id}/pause|resume|interrupt|terminate`. All calls live in `src/api/`.

## How the browser writes to the backend

The backend has no CORS configuration, so the browser never calls it. The forms use React form actions backed by
Next.js **Server Actions** (`src/app/supervisors/new/actions.ts`, `src/app/runs/new/actions.ts`, `src/app/runs/[runId]/actions.ts`): the browser posts to
the Next.js server, and the action calls FastAPI from there. Validation stays authoritative in the backend; the forms
only catch obvious mistakes early. Backend errors are turned into readable messages (`src/api/messages.ts`), and the
submit button is disabled while a request is in flight.

Creating a supervisor with an existing name creates the next *version* (versions are immutable). A run that failed to
start keeps its order ID, so retrying the same order ID reports "already exists" (existing backend behaviour).
