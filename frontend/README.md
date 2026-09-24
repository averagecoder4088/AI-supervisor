# Order Supervisor: frontend

Next.js (App Router) + TypeScript + Tailwind CSS. A simple dashboard over the existing FastAPI backend.
It only *reads* from the backend for now; supervisor creation, event injection and run controls come later.

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
| `/runs/[runId]` | Basic run information |

## Backend endpoints used

`GET /api/runs`, `GET /api/runs/{run_id}`, `GET /api/supervisors/{supervisor_id}`. All calls live in `src/api/`.
