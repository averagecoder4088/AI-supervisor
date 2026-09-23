# Order Supervisor

An AI supervisor POC that oversees an order throughout its lifecycle using Temporal durable workflows, FastAPI, and structured LLM reasoning.

For full architectural details, refer to the governing specifications in [`DOCS/`](DOCS/).

---

## Backend Setup & Local Development

### 1. Prerequisites

- Python 3.9+
- `venv` module

### 2. Environment Setup

From the repository root:

```bash
# Create a virtual environment
python3 -m venv .venv

# Activate the virtual environment
source .venv/bin/activate

# Install backend dependencies
pip install -r backend/requirements.txt
```

### 3. Configuration

Copy the example environment configuration:

```bash
cp .env.example .env
```

Configuration variables in `.env`:

| Variable | Description | Default |
|---|---|---|
| `APP_ENV` | Application environment (`development`, `staging`, `production`, `test`) | `development` |
| `DEBUG` | Enable debug mode and automatic reload | `false` |
| `HOST` | Server bind host address | `127.0.0.1` |
| `PORT` | Server bind port | `8000` |
| `TEMPORAL_ADDRESS` | Temporal server address used by the worker | `localhost:7233` |
| `TEMPORAL_NAMESPACE` | Temporal namespace used by the worker | `default` |

### 4. Start the Development Server

From the repository root:

```bash
PYTHONPATH=backend uvicorn app.main:app --reload --port 8000
```

Alternatively, from the `backend/` directory:

```bash
cd backend
uvicorn app.main:app --reload --port 8000
```

### 5. Verify the Health Endpoint

With the server running, send a request to the health check endpoint:

```bash
curl http://127.0.0.1:8000/health
```

Expected response:

```json
{"status":"ok","app_env":"development"}
```

### 6. Run Tests

To run the test suite:

```bash
PYTHONPATH=backend pytest backend/tests
```
