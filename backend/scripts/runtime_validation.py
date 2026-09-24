"""Step 9A - real Temporal runtime validation. Manual tooling; NOT part of the pytest suite.

Proves the sealed architecture works as a running system, with genuinely separate
processes and no test-server shortcuts:

    driver -> FastAPI (uvicorn) -> Temporal client -> real Temporal dev server
                                                       <- real worker (production create_worker,
                                                          FakeLLMClient injected, real mock tools)
    worker Activities and the API -> PostgreSQL

The scenario is the sealed S1 "smooth delivery" (same supervisor body, same scripted LLM, same
external-world simulator). Only the wake interval is adjusted, 60 -> 1 minute, so the real
durable timer can be waited for in practical time. No real OpenAI call is made.

Usage (from the project root; nothing needs PYTHONPATH):

    .venv/bin/python backend/scripts/runtime_validation.py preflight
    .venv/bin/python backend/scripts/runtime_validation.py all [--keep] [--evidence-dir DIR]
    .venv/bin/python backend/scripts/runtime_validation.py cleanup [--evidence-dir RUN_DIR]

    # or one component per terminal, then the driver:
    ... server        # Temporal dev server on the configured address, UI on port+1000
    ... worker --llm-log LOG
    (cd backend && ../.venv/bin/uvicorn app.main:app --port 8000)
    ... scenario --llm-log LOG

Exit codes: 0 all hard checks passed, 1 a hard check failed, 2 preflight/environment problem,
3 cleanup could not restore the pre-test state.

Evidence is written OUTSIDE the repository (default ~/order-supervisor-step9-evidence/<UTC time>/).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import glob
import importlib.metadata
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
os.chdir(BACKEND)  # Settings look for .env / backend/.env relative to the working directory
sys.path.insert(0, str(BACKEND))

import httpx  # noqa: E402
from sqlalchemy import delete, select, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from temporalio.api.enums.v1 import EventType, TaskQueueType  # noqa: E402
from temporalio.api.taskqueue.v1 import TaskQueue  # noqa: E402
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest, GetSystemInfoRequest  # noqa: E402
from temporalio.client import Client, WorkflowExecutionStatus  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.mock_models import MockCustomerMessage, MockOrder, MockShipment  # noqa: E402
from app.db.models import (  # noqa: E402
    Action,
    Event,
    FinalOutput,
    MemorySnapshot,
    Run,
    Supervisor,
    TimelineEntry,
    ToolExecution,
)
from app.llm.fake import FakeLLMClient  # noqa: E402
from app.llm.schemas import FINAL_OUTPUT_SCHEMA_NAME, REASONING_SCHEMA_NAME  # noqa: E402
from app.simulation.s1_smooth_delivery import s1_llm_script, s1_supervisor_body  # noqa: E402
from app.simulation.world import ExternalWorld, shipment_id_for  # noqa: E402
from app.temporal.constants import TASK_QUEUE, order_workflow_id  # noqa: E402
from app.temporal.worker import create_worker  # noqa: E402
from app.temporal.workflows import OrderWorkflow  # noqa: E402

PREFIX = "API-TEST-S9-"
ORDER_ID = f"{PREFIX}001"
WORKFLOW_ID = order_workflow_id(ORDER_ID)
SUPERVISOR_NAME = f"{PREFIX}S1 Smooth Delivery (real runtime)"
ROUTINE_EVENTS = ("order_created", "payment_confirmed", "shipment_created")
WAKE_MINUTES = 1  # the ONLY change to S1: the scheduled wake, 60 -> 1 minute
TABLES = [
    "supervisors", "runs", "events", "timeline_entries", "actions", "tool_executions",
    "memory_snapshots", "final_outputs", "mock_orders", "mock_shipments", "mock_customer_messages",
]
# What the workflow code predicts for this scenario. Reported against the real history; a
# difference is investigated and reported, never silently accepted or silently failed.
PREDICTED_ACTIVITIES = {
    "record_event": 4, "record_timeline_entries": 3, "record_action_started": 2, "record_action_finished": 2,
    "save_memory_snapshot": 2, "generate_reasoning_decision": 2, "execute_tool": 2,
    "generate_final_output": 1, "complete_run": 1,
}
DEFAULT_EVIDENCE_ROOT = Path("~/order-supervisor-step9-evidence").expanduser()


# ============================================================== small utilities


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def stamp() -> str:
    return now_utc().strftime("%Y%m%dT%H%M%SZ")


def say(message: str) -> None:
    print(message, flush=True)


def port_open(host: str, port: int) -> bool:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


async def eventually(check: Callable[[], Awaitable[Any]], *, timeout: float, what: str, interval: float = 0.25) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = await check()
        if last:
            return last
        await asyncio.sleep(interval)
    raise TimeoutError(f"timed out after {timeout:.0f}s waiting for: {what} (last value: {last!r})")


_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def norm(value: Any) -> Any:
    """Normalise datetimes (objects or ISO strings) so JSON and dataclass views compare equal."""
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    if isinstance(value, str) and _ISO.match(value):
        try:
            return norm(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return value
    if isinstance(value, dict):
        return {k: norm(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [norm(v) for v in value]
    return value


def try_json(raw: Any) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw if isinstance(raw, str) else None


def pgrep(pattern: str) -> List[int]:
    result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    mine = {os.getpid(), os.getppid()}
    return [int(p) for p in result.stdout.split() if int(p) not in mine]


class Evidence:
    """Everything is written under one directory OUTSIDE the repository."""

    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "logs").mkdir(parents=True, exist_ok=True)
        (root / "diagnostics").mkdir(parents=True, exist_ok=True)

    def json(self, name: str, data: Any) -> None:
        (self.root / name).write_text(json.dumps(data, indent=2, default=str) + "\n")

    def text(self, name: str, content: str) -> None:
        (self.root / name).write_text(content)

    def jsonl(self, name: str, obj: Any) -> None:
        with open(self.root / name, "a") as handle:
            handle.write(json.dumps(obj, default=str) + "\n")


class Report:
    """hard check -> PASS/FAIL; note -> PASS/NOTE (a reported difference, not a product failure)."""

    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []

    def check(self, name: str, ok: bool, detail: str = "", *, kind: str = "hard") -> bool:
        status = "PASS" if ok else ("FAIL" if kind == "hard" else "NOTE")
        self.rows.append({"status": status, "kind": kind, "check": name, "detail": detail})
        say(f"  [{status}] {name}" + (f" -- {detail}" if detail else ""))
        return ok

    @property
    def failures(self) -> List[Dict[str, Any]]:
        return [r for r in self.rows if r["status"] == "FAIL"]

    @property
    def notes(self) -> List[Dict[str, Any]]:
        return [r for r in self.rows if r["status"] == "NOTE"]


# ================================================================= database


def make_engine():
    return create_async_engine(get_settings().database_url)


async def table_counts(engine) -> Dict[str, int]:
    async with engine.connect() as conn:
        return {t: (await conn.execute(text(f"select count(*) from {t}"))).scalar() for t in TABLES}


async def prefix_rows(engine) -> Dict[str, int]:
    async with engine.connect() as conn:
        queries = {
            "runs": "select count(*) from runs where order_id like :p",
            "supervisors": "select count(*) from supervisors where name like :p",
            "mock_orders": "select count(*) from mock_orders where order_id like :p",
        }
        return {k: (await conn.execute(text(q), {"p": f"{PREFIX}%"})).scalar() for k, q in queries.items()}


async def alembic_state(engine) -> Dict[str, Optional[str]]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    async with engine.connect() as conn:
        current = (await conn.execute(text("select version_num from alembic_version"))).scalar()
    return {"current": current, "head": head}


async def cleanup_database(engine) -> None:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as session:
        # Runs first (their history cascades); supervisors are RESTRICTed by runs; mock shipments
        # and messages cascade from mock_orders. Only the API-TEST-S9- namespace is touched.
        await session.execute(delete(Run).where(Run.order_id.like(f"{PREFIX}%")))
        await session.execute(delete(Supervisor).where(Supervisor.name.like(f"{PREFIX}%")))
        await session.execute(delete(MockOrder).where(MockOrder.order_id.like(f"{PREFIX}%")))
        await session.commit()


# ================================================================ preflight


async def preflight(*, expect_components_running: bool) -> Dict[str, Any]:
    settings = get_settings()
    host, _, port = settings.temporal_address.rpartition(":")
    temporal_port, ui_port, api_port = int(port), int(port) + 1000, settings.port
    problems: List[str] = []
    info: Dict[str, Any] = {
        "python": sys.version.split()[0],
        "temporalio": importlib.metadata.version("temporalio"),
        "temporal_address": settings.temporal_address,
        "namespace": settings.temporal_namespace,
        "ports": {"temporal": temporal_port, "ui": ui_port, "api": api_port},
    }
    if host not in ("localhost", "127.0.0.1"):
        problems.append(f"TEMPORAL_ADDRESS host {host!r} is not local; this validation starts a local dev server")
    engine = make_engine()
    try:
        info["alembic"] = await alembic_state(engine)
        if info["alembic"]["current"] != info["alembic"]["head"]:
            problems.append(f"Alembic current {info['alembic']['current']} != head {info['alembic']['head']}")
        info["table_counts_before"] = await table_counts(engine)
        info["prefix_rows_before"] = await prefix_rows(engine)
        if any(info["prefix_rows_before"].values()):
            problems.append(f"leftover {PREFIX}* rows exist (run `cleanup`): {info['prefix_rows_before']}")
    except Exception as err:  # noqa: BLE001
        problems.append(f"PostgreSQL not usable: {type(err).__name__}: {err}")
    finally:
        await engine.dispose()
    if not expect_components_running:
        for name, p in (("Temporal", temporal_port), ("Temporal UI", ui_port), ("API", api_port)):
            if port_open("127.0.0.1", p):
                problems.append(f"port {p} ({name}) is already in use")
    for label, pattern in (
        ("pytest (its cleanup would delete API-TEST-* rows)", r"-m pytest|/pytest( |$)"),
        ("another runtime_validation process", r"runtime_validation\.py"),
    ):
        pids = pgrep(pattern)
        if pids and not (expect_components_running and "runtime_validation" in label):
            problems.append(f"{label} is running: pids {pids}")
    cached = glob.glob(os.path.join(tempfile.gettempdir(), "temporal-cli*")) + glob.glob(
        os.path.join(tempfile.gettempdir(), "temporal_cli*")
    )
    info["dev_server_binary_cached"] = bool(cached)
    return {"info": info, "problems": problems}


# ============================================================ server / worker


async def wait_for_signal() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()


async def cmd_server(args: argparse.Namespace) -> int:
    settings = get_settings()
    port = int(settings.temporal_address.rpartition(":")[2])
    env = await WorkflowEnvironment.start_local(
        namespace=settings.temporal_namespace, ip="127.0.0.1", port=port, ui=True, dev_server_log_level="warn"
    )
    say(f"TEMPORAL_SERVER_READY pid={os.getpid()} address=127.0.0.1:{port} ui=http://127.0.0.1:{port + 1000}")
    await wait_for_signal()
    await env.shutdown()
    say("TEMPORAL_SERVER_STOPPED")
    return 0


class RecordingFakeLLM(FakeLLMClient):
    """The unchanged FakeLLMClient plus an append-only call log (the worker is a separate process)."""

    def __init__(self, responses: List[str], log_path: Path) -> None:
        super().__init__(responses)
        self._log_path = log_path

    async def generate_json(self, *, system_prompt, user_prompt, schema_name, json_schema):  # type: ignore[override]
        prompt = try_json(user_prompt) or {}
        entry: Dict[str, Any] = {"at": now_utc().isoformat(), "pid": os.getpid(), "schema_name": schema_name}
        if schema_name == REASONING_SCHEMA_NAME:
            entry.update(
                wake_reason=prompt.get("wake_reason"),
                order_status=prompt.get("order_status"),
                new_event_types=[e.get("event_type") for e in prompt.get("new_events", [])],
                memory_summary=(prompt.get("memory") or {}).get("situation_summary"),
                last_action_tool=(prompt.get("last_action_result") or {}).get("tool"),
            )
        else:
            entry.update(
                final_order_status=prompt.get("final_order_status"),
                events_received=prompt.get("events_received"),
                reasoning_count=prompt.get("reasoning_count"),
                recent_event_types=[e.get("event_type") for e in prompt.get("recent_events", [])],
                action_tools=[a.get("tool") for a in prompt.get("action_log", [])],
            )
        with open(self._log_path, "a") as handle:
            handle.write(json.dumps(entry) + "\n")
        return await super().generate_json(
            system_prompt=system_prompt, user_prompt=user_prompt, schema_name=schema_name, json_schema=json_schema
        )


def runtime_llm_script() -> List[str]:
    """S1's scripted LLM, verbatim, except each decision asks for the 1-minute wake."""
    script = []
    for raw in s1_llm_script():
        data = json.loads(raw)
        if "next_wake_in_minutes" in data:
            data["next_wake_in_minutes"] = WAKE_MINUTES
        script.append(json.dumps(data))
    return script


async def cmd_worker(args: argparse.Namespace) -> int:
    settings = get_settings()
    log_path = Path(args.llm_log).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("")
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    # The production wiring (create_worker -> real persistence + real mock tools); only the LLM is injected.
    worker = create_worker(client, llm_client=RecordingFakeLLM(runtime_llm_script(), log_path))
    async with worker:
        say(f"WORKER_READY pid={os.getpid()} task_queue={TASK_QUEUE} llm=RecordingFakeLLM(scripted, 3 calls)")
        await wait_for_signal()
    say("WORKER_STOPPED")
    return 0


# ============================================================ history analysis


def analyse_history(events: List[Any]) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    scheduled: Dict[int, str] = {}
    out: Dict[str, Any] = {
        "activities_scheduled": {}, "activities_completed": {}, "activities_failed": {}, "activity_max_attempt": 0,
        "started_identities": set(), "signals": [], "timers": {"started": 0, "fired": 0, "canceled": 0},
        "workflow_task_failed": 0, "workflow_started": None,
    }
    for ev in events:
        etype = EventType.Name(ev.event_type).replace("EVENT_TYPE_", "")
        at = ev.event_time.ToDatetime().replace(tzinfo=timezone.utc)
        which = ev.WhichOneof("attributes")
        a = getattr(ev, which) if which else None
        row: Dict[str, Any] = {"id": ev.event_id, "at": at, "type": etype, "detail": ""}
        if etype == "ACTIVITY_TASK_SCHEDULED":
            name = a.activity_type.name
            scheduled[ev.event_id] = name
            out["activities_scheduled"][name] = out["activities_scheduled"].get(name, 0) + 1
            row.update(activity=name, detail=f"{name} task_queue={a.task_queue.name}")
        elif etype == "ACTIVITY_TASK_STARTED":
            name = scheduled.get(a.scheduled_event_id, "?")
            out["started_identities"].add(a.identity)
            out["activity_max_attempt"] = max(out["activity_max_attempt"], a.attempt)
            row.update(activity=name, identity=a.identity, detail=f"{name} attempt={a.attempt} identity={a.identity}")
        elif etype == "ACTIVITY_TASK_COMPLETED":
            name = scheduled.get(a.scheduled_event_id, "?")
            out["activities_completed"][name] = out["activities_completed"].get(name, 0) + 1
            row.update(activity=name, detail=name)
        elif etype in ("ACTIVITY_TASK_FAILED", "ACTIVITY_TASK_TIMED_OUT"):
            name = scheduled.get(a.scheduled_event_id, "?")
            out["activities_failed"][name] = out["activities_failed"].get(name, 0) + 1
            row.update(activity=name, detail=f"{name} {etype}")
        elif etype == "TIMER_STARTED":
            out["timers"]["started"] += 1
            row["detail"] = f"timer_id={a.timer_id} start_to_fire={a.start_to_fire_timeout.ToTimedelta()}"
        elif etype == "TIMER_FIRED":
            out["timers"]["fired"] += 1
            row["detail"] = f"started_event_id={a.started_event_id}"
        elif etype == "TIMER_CANCELED":
            out["timers"]["canceled"] += 1
        elif etype == "WORKFLOW_EXECUTION_SIGNALED":
            payload = try_json(a.input.payloads[0].data.decode()) if a.input.payloads else None
            event_type = payload.get("event_type") if isinstance(payload, dict) else None
            out["signals"].append({"id": ev.event_id, "at": at, "name": a.signal_name, "identity": a.identity,
                                   "event_type": event_type})
            row.update(identity=a.identity, detail=f"{a.signal_name} event_type={event_type} identity={a.identity}")
        elif etype == "WORKFLOW_EXECUTION_STARTED":
            out["workflow_started"] = {"identity": a.identity, "task_queue": a.task_queue.name,
                                       "workflow_type": a.workflow_type.name}
            row.update(identity=a.identity, detail=f"{a.workflow_type.name} task_queue={a.task_queue.name} identity={a.identity}")
        elif etype == "WORKFLOW_TASK_STARTED":
            row.update(identity=a.identity, detail=f"identity={a.identity}")
        elif etype == "WORKFLOW_TASK_FAILED":
            out["workflow_task_failed"] += 1
        rows.append(row)
    out["rows"] = rows
    out["final_event_type"] = rows[-1]["type"] if rows else None
    out["total_activities_scheduled"] = sum(out["activities_scheduled"].values())
    return out


def history_table(analysis: Dict[str, Any]) -> str:
    lines = [f"{'id':>4}  {'time (UTC)':<27} {'event':<34} detail"]
    for r in analysis["rows"]:
        lines.append(f"{r['id']:>4}  {r['at'].isoformat():<27} {r['type']:<34} {r['detail']}")
    return "\n".join(lines) + "\n"


def wake_trigger_of_reasoning(analysis: Dict[str, Any], index: int) -> Optional[Dict[str, Any]]:
    """The latest wake-relevant history event before the index-th reasoning Activity was scheduled."""
    reasoning = [r for r in analysis["rows"] if r["type"] == "ACTIVITY_TASK_SCHEDULED"
                 and r.get("activity") == "generate_reasoning_decision"]
    if index >= len(reasoning):
        return None
    cutoff = reasoning[index]["id"]
    for r in reversed([r for r in analysis["rows"] if r["id"] < cutoff]):
        if r["type"] in ("TIMER_FIRED", "WORKFLOW_EXECUTION_SIGNALED", "WORKFLOW_EXECUTION_STARTED"):
            return {"type": r["type"], "id": r["id"], "at": r["at"], "scheduled_at": reasoning[index]["at"]}
    return None


# ================================================================== scenario


class Scenario:
    def __init__(self, args: argparse.Namespace, evidence: Evidence, report: Report) -> None:
        self.args, self.evidence, self.report = args, evidence, report
        self.settings = get_settings()
        self.api_port = args.api_port or self.settings.port
        self.llm_log = Path(args.llm_log).expanduser()
        self.status_pairs: List[Dict[str, Any]] = []
        self.timing: Dict[str, Any] = {}
        self.run_id = ""

    # ---------------------------------------------------------- llm log
    def llm_calls(self) -> List[Dict[str, Any]]:
        if not self.llm_log.exists():
            return []
        return [json.loads(line) for line in self.llm_log.read_text().splitlines() if line.strip()]

    # ---------------------------------------------------- api / temporal
    async def _log_response(self, response: httpx.Response) -> None:
        await response.aread()
        request = response.request
        self.evidence.jsonl("api_transcript.jsonl", {
            "at": now_utc().isoformat(), "method": request.method, "path": request.url.raw_path.decode(),
            "request": try_json(request.content.decode()) if request.content else None,
            "status": response.status_code, "response": try_json(response.text),
        })

    async def status_pair(self, label: str) -> Dict[str, Any]:
        """FastAPI GET /status vs a direct Temporal query from an independent client."""
        response = await self.http.get(f"/api/runs/{self.run_id}/status")
        api = response.json()
        direct = dataclasses.asdict(await self.handle.query(OrderWorkflow.get_status))
        differing = sorted(k for k in set(api) | set(direct) if norm(api.get(k)) != norm(direct.get(k)))
        self.status_pairs.append({"at": now_utc().isoformat(), "label": label, "http_status": response.status_code,
                                  "api": api, "direct_query": direct, "differing_fields": differing})
        self.report.check(f"/status agrees with direct Temporal query @ {label}",
                          response.status_code == 200 and not differing,
                          f"http={response.status_code}" + (f", differing: {differing}" if differing else ""))
        return api

    async def settled(self, cycles: int, timeout: float) -> Dict[str, Any]:
        async def check():
            r = await self.http.get(f"/api/runs/{self.run_id}/status")
            if r.status_code != 200:
                return None
            s = r.json()
            return s if s["reasoning_count"] == cycles and s["state"] == "sleeping" else None

        return await eventually(check, timeout=timeout, what=f"{cycles} reasoning cycle(s) settled and sleeping")

    async def recorded(self, event_type: str) -> None:
        expected = f"Event received: {event_type} (recorded; does not wake the supervisor)"

        async def check():
            entries = (await self.http.get(f"/api/runs/{self.run_id}/timeline")).json()["entries"]
            return any(e["message"].startswith(expected) for e in entries)

        await eventually(check, timeout=30, what=f"workflow recorded {event_type}")

    async def mock_state(self) -> Dict[str, List[Dict[str, Any]]]:
        out: Dict[str, List[Dict[str, Any]]] = {}
        async with self.factory() as session:
            for name, model in (("orders", MockOrder), ("shipments", MockShipment), ("messages", MockCustomerMessage)):
                rows = (await session.execute(select(model).where(model.order_id == ORDER_ID))).scalars().all()
                out[name] = [{c.name: getattr(r, c.name) for c in r.__table__.columns} for r in rows]
        return out

    async def pollers(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for name, kind in (("workflow", TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW), ("activity", TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY)):
            try:
                resp = await self.client.workflow_service.describe_task_queue(DescribeTaskQueueRequest(
                    namespace=self.client.namespace, task_queue=TaskQueue(name=TASK_QUEUE), task_queue_type=kind))
                out[name] = [{"identity": p.identity, "last_access_time":
                              p.last_access_time.ToDatetime().isoformat() + "Z"} for p in resp.pollers]
            except Exception as err:  # noqa: BLE001
                out[name] = {"error": f"{type(err).__name__}: {err}"}
        return out

    # ------------------------------------------------------------- main
    async def run(self) -> None:
        r, e = self.report, self.evidence
        engine = make_engine()
        self.factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        self.client = await Client.connect(self.settings.temporal_address, namespace=self.settings.temporal_namespace)
        self.http = httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.api_port}", timeout=30,
                                      event_hooks={"response": [self._log_response]})
        try:
            await self._scenario(engine)
        finally:
            await self.http.aclose()
            await engine.dispose()

    async def _scenario(self, engine) -> None:
        r, e = self.report, self.evidence
        world = ExternalWorld(self.factory, self.http)

        say("\n== environment")
        health = await self.http.get("/health")
        r.check("FastAPI answers GET /health", health.status_code == 200, f"http={health.status_code}")
        system = await self.client.workflow_service.get_system_info(GetSystemInfoRequest())
        e.json("versions_runtime.json", {"temporal_server_version": system.server_version,
                                         "client_identity": self.client.identity})
        ui = None
        with contextlib.suppress(Exception):
            ui = httpx.get(f"http://127.0.0.1:{int(self.settings.temporal_address.rpartition(':')[2]) + 1000}/", timeout=5)
        r.check("Temporal UI answers on port+1000", ui is not None and ui.status_code == 200,
                f"http={getattr(ui, 'status_code', None)}", kind="note")
        pollers = await self.pollers()
        e.json("task_queue_pollers_before_run.json", pollers)
        r.check("worker is polling the workflow task queue", isinstance(pollers["workflow"], list) and len(pollers["workflow"]) > 0,
                f"{TASK_QUEUE}: {pollers['workflow']}")
        r.check("worker is polling the activity task queue", isinstance(pollers["activity"], list) and len(pollers["activity"]) > 0,
                f"{TASK_QUEUE}: {pollers['activity']}")

        say("\n== supervisor and run creation (through the real API)")
        body = s1_supervisor_body(SUPERVISOR_NAME)
        body["default_wake_interval_minutes"] = WAKE_MINUTES  # S1 with the wake shortened; min stays 1
        created = await self.http.post("/api/supervisors", json=body)
        r.check("POST /api/supervisors -> 201", created.status_code == 201, created.text[:200])
        supervisor_id = created.json()["id"]
        await world.place_order(ORDER_ID)
        placed = await self.mock_state()
        r.check("mock order placed (status created), no shipment, no messages",
                [o["status"] for o in placed["orders"]] == ["created"] and not placed["shipments"] and not placed["messages"])
        response = await self.http.post("/api/runs", json={"order_id": ORDER_ID, "supervisor_id": supervisor_id})
        run = response.json()
        r.check("POST /api/runs -> 201 running", response.status_code == 201 and run.get("status") == "running",
                f"http={response.status_code} status={run.get('status')} order_status={run.get('order_status')}")
        self.run_id = run["id"]
        self.timing["run_created_at"] = now_utc()
        self.handle = self.client.get_workflow_handle_for(OrderWorkflow.run, WORKFLOW_ID)
        desc = await self.handle.describe()
        r.check("a real OrderWorkflow is RUNNING on the real server", desc.status == WorkflowExecutionStatus.RUNNING
                and desc.workflow_type == "OrderWorkflow" and desc.task_queue == TASK_QUEUE,
                f"id={WORKFLOW_ID} status={desc.status.name} type={desc.workflow_type} task_queue={desc.task_queue}")

        say("\n== workflow_start reasoning")
        start = await self.settled(1, timeout=60)
        self.timing["cycle1_settled_at"] = now_utc()
        r.check("workflow_start cycle ran once", start["last_wake_reason"] == "workflow_start"
                and start["events_received"] == 0 and start["order_status"] is None, f"{start['last_wake_reason']}")
        r.check("cycle 1 memory came from the scripted LLM, tool get_order_status succeeded",
                start["memory"]["situation_summary"] == "Order placed; awaiting payment"
                and start["memory"]["last_action"] == "get_order_status: succeeded")
        await self.status_pair("after workflow_start cycle")
        wake1 = norm(start["next_wake_at"])
        self.timing["scheduled_wake_at"] = wake1
        r.check("one LLM call so far (worker log)", len(self.llm_calls()) == 1)

        say("\n== routine events through the API: recorded, no reasoning")
        expected_status = {"order_created": "created", "payment_confirmed": "payment_confirmed", "shipment_created": "shipped"}
        world_steps = {"order_created": world.order_created, "payment_confirmed": world.confirm_payment,
                       "shipment_created": world.create_shipment}
        for event_type in ROUTINE_EVENTS:
            await world_steps[event_type](self.run_id, ORDER_ID)
            await self.recorded(event_type)
            s = await self.status_pair(f"after {event_type}")
            mock_status = (await self.mock_state())["orders"][0]["status"]
            r.check(f"{event_type}: no unintended reasoning, timer untouched, status follows the world",
                    s["reasoning_count"] == 1 and s["state"] == "sleeping" and norm(s["next_wake_at"]) == wake1
                    and s["order_status"] == expected_status[event_type] == mock_status and len(self.llm_calls()) == 1,
                    f"reasoning_count={s['reasoning_count']} order_status={s['order_status']}/{mock_status}")
        remaining = (wake1 - now_utc()).total_seconds()
        r.check("timing precondition: all routine events arrived >20s before the scheduled wake", remaining > 20,
                f"{remaining:.0f}s to spare")
        if remaining <= 20:
            raise RuntimeError("timing precondition violated; the scripted sequence would no longer apply")

        say("\n== real durable timer")
        await asyncio.sleep(max(0.0, (wake1 - timedelta(seconds=30) - now_utc()).total_seconds()))
        s = await self.status_pair("~30s before the scheduled wake")
        r.check("no early wake (still 1 cycle, 1 LLM call)", s["reasoning_count"] == 1 and len(self.llm_calls()) == 1)
        woke = await self.settled(2, timeout=(wake1 - now_utc()).total_seconds() + 90)
        self.timing["cycle2_settled_at"] = now_utc()
        r.check("scheduled wake ran a second cycle (get_shipment_status)", woke["last_wake_reason"] == "scheduled_wakeup"
                and woke["memory"]["last_action"] == "get_shipment_status: succeeded" and woke["pending_events"] == [],
                f"reason={woke['last_wake_reason']}")
        await self.status_pair("after the scheduled cycle")
        calls = self.llm_calls()
        r.check("cycle 2 prompt: scheduled_wakeup with exactly the three routine events and cycle-1 memory",
                len(calls) == 2 and calls[1]["wake_reason"] == "scheduled_wakeup"
                and calls[1]["new_event_types"] == list(ROUTINE_EVENTS)
                and calls[1]["memory_summary"] == "Order placed; awaiting payment", str(calls[1:] and calls[1]))

        say("\n== delivered -> terminal handling")
        await world.deliver(self.run_id, ORDER_ID)
        self.timing["delivered_sent_at"] = now_utc()
        result = await asyncio.wait_for(self.handle.result(), timeout=90)
        self.timing["workflow_returned_at"] = now_utc()

        say("\n== Temporal completion (independent of PostgreSQL)")
        desc = await self.handle.describe()
        e.json("workflow_describe.json", {
            "workflow_id": WORKFLOW_ID, "run_id": desc.run_id, "status": desc.status.name, "type": desc.workflow_type,
            "task_queue": desc.task_queue, "start_time": desc.start_time, "close_time": desc.close_time,
            "history_length": desc.history_length})
        r.check("Temporal WorkflowExecutionStatus is COMPLETED", desc.status == WorkflowExecutionStatus.COMPLETED,
                f"status={desc.status.name} history_length={desc.history_length}")
        r.check("workflow result: 2 cycles, 4 events, delivered, terminal, final output persisted",
                (result.reasoning_count, result.events_received, result.order_status, result.state,
                 result.terminal_order_status_reached, result.final_output_persisted)
                == (2, 4, "delivered", "terminal", True, True), f"{result.reasoning_count=} {result.state=}")
        closed = await self.http.get(f"/api/runs/{self.run_id}/status")
        r.check("after close, GET /status -> 409 RUN_NOT_ACTIVE (existing behaviour)",
                closed.status_code == 409 and closed.json().get("code") == "RUN_NOT_ACTIVE", closed.text[:120])

        history = await self.handle.fetch_history()
        e.text("temporal_history.json", history.to_json())
        analysis = analyse_history(list(history.events))
        e.text("temporal_history_table.txt", history_table(analysis))
        self.timing["history"] = analysis
        await self._check_history(analysis, wake1)
        await self._check_llm_log()
        await self._check_database(engine)
        await self._check_observation()
        e.json("status_comparisons.json", self.status_pairs)
        e.json("task_queue_pollers_after_run.json", await self.pollers())
        r.check("no leftover unexpected LLM calls (exactly 3)", len(self.llm_calls()) == 3)

    # -------------------------------------------------------- checks
    async def _check_history(self, a: Dict[str, Any], wake1: datetime) -> None:
        r, e = self.report, self.evidence
        say("\n== Activities and timer, from the real server's event history")
        done, sched = a["activities_completed"], a["activities_scheduled"]
        semantic = {"record_event": 4, "record_action_started": 2, "record_action_finished": 2, "save_memory_snapshot": 2,
                    "generate_reasoning_decision": 2, "execute_tool": 2, "generate_final_output": 1, "complete_run": 1}
        for name, expected in semantic.items():
            r.check(f"Activity {name}: {expected} scheduled and completed through Temporal",
                    sched.get(name) == expected and done.get(name) == expected, f"scheduled={sched.get(name)} completed={done.get(name)}")
        r.check("record_timeline_entries executed (batch count reported below)", done.get("record_timeline_entries", 0) >= 1)
        r.check("no Activity failed or timed out, none retried", not a["activities_failed"] and a["activity_max_attempt"] <= 1,
                f"failed={a['activities_failed']} max_attempt={a['activity_max_attempt']}")
        r.check("no workflow task failures (deterministic replay)", a["workflow_task_failed"] == 0)
        r.check("history is a single OrderWorkflow on the correct task queue", a["workflow_started"] is not None
                and a["workflow_started"]["workflow_type"] == "OrderWorkflow" and a["workflow_started"]["task_queue"] == TASK_QUEUE,
                str(a["workflow_started"]))
        r.check("history ends with WorkflowExecutionCompleted", a["final_event_type"] == "WORKFLOW_EXECUTION_COMPLETED",
                str(a["final_event_type"]))
        signals = [s for s in a["signals"] if s["name"] == "submit_event"]
        r.check("four submit_event Signals arrived, in order", [s["event_type"] for s in signals]
                == [*ROUTINE_EVENTS, "delivered"], str([s["event_type"] for s in signals]))
        identities = {"worker_activity_identities": sorted(a["started_identities"]),
                      "api_workflow_start_identity": (a["workflow_started"] or {}).get("identity"),
                      "api_signal_identities": sorted({s["identity"] for s in signals}), "driver_client_identity": self.client.identity}
        e.json("identities.json", identities)
        r.check("Activities ran under a different identity than the API that started the workflow",
                bool(a["started_identities"]) and not (a["started_identities"] & {identities["api_workflow_start_identity"]}),
                json.dumps(identities), kind="note")
        # The timer, from history: wake #1 came from the workflow start, wake #2 from a TimerFired.
        first, second = wake_trigger_of_reasoning(a, 0), wake_trigger_of_reasoning(a, 1)
        r.check("reasoning #1 was triggered by the workflow start", first is not None and first["type"] == "WORKFLOW_EXECUTION_STARTED", str(first))
        r.check("reasoning #2 was triggered by a TimerFired event, not by a Signal", second is not None and second["type"] == "TIMER_FIRED", str(second))
        if second and second["type"] == "TIMER_FIRED":
            lateness = (second["at"] - wake1).total_seconds()
            self.timing["timer_fired_at"] = second["at"]
            self.timing["timer_lateness_seconds"] = lateness
            last_routine = max(s["at"] for s in signals if s["event_type"] in ROUTINE_EVENTS)
            r.check("the TimerFired happened after the last routine Signal and at about the scheduled wake time",
                    second["at"] > last_routine and -2 <= lateness <= 20,
                    f"fired {lateness:+.1f}s relative to next_wake_at", kind="note")
        after_delivered = [row for row in a["rows"] if row["type"] == "ACTIVITY_TASK_SCHEDULED"
                           and row["id"] > signals[-1]["id"]] if signals else []
        r.check("after `delivered`: no reasoning Activity, final output then complete_run",
                [x.get("activity") for x in after_delivered if x.get("activity") in ("generate_reasoning_decision", "generate_final_output", "complete_run")]
                == ["generate_final_output", "complete_run"], str([x.get("activity") for x in after_delivered]))
        # Reported comparison against the workflow code's prediction (not an invariant).
        differences = {k: {"predicted": v, "actual": sched.get(k, 0)} for k, v in PREDICTED_ACTIVITIES.items() if sched.get(k, 0) != v}
        extras = {k: v for k, v in sched.items() if k not in PREDICTED_ACTIVITIES}
        r.check("Activity breakdown equals the workflow-code prediction", not differences and not extras,
                f"total={a['total_activities_scheduled']} scheduled={sched}" + (f"; DIFFERENCES: {differences} extras: {extras}" if differences or extras else ""),
                kind="note")
        e.json("activity_summary.json", {"scheduled": sched, "completed": done, "failed": a["activities_failed"],
                                         "predicted": PREDICTED_ACTIVITIES, "differences": differences, "extras": extras,
                                         "timers": a["timers"], "total_scheduled": a["total_activities_scheduled"]})

    async def _check_llm_log(self) -> None:
        r = self.report
        calls = self.llm_calls()
        say("\n== FakeLLMClient through the real Activity path (worker call log)")
        r.check("worker log: reasoning, reasoning, final_output", [c["schema_name"] for c in calls]
                == [REASONING_SCHEMA_NAME, REASONING_SCHEMA_NAME, FINAL_OUTPUT_SCHEMA_NAME], str([c["schema_name"] for c in calls]))
        if len(calls) == 3:
            r.check("calls came from the worker process, not the driver or API", len({c["pid"] for c in calls}) == 1
                    and calls[0]["pid"] != os.getpid(), f"worker pid {calls[0]['pid']} driver pid {os.getpid()}")
            r.check("call 1 = workflow_start with no events; call 3 = final output for a delivered order",
                    calls[0]["wake_reason"] == "workflow_start" and calls[0]["new_event_types"] == []
                    and calls[2]["final_order_status"] == "delivered" and calls[2]["reasoning_count"] == 2
                    and calls[2]["events_received"] == 4)

    async def _check_database(self, engine) -> None:
        r, e = self.report, self.evidence
        say("\n== PostgreSQL final state")
        state: Dict[str, Any] = {}
        async with self.factory() as session:
            rid = uuid.UUID(self.run_id)
            run = await session.get(Run, rid)
            r.check("runs: completed, order_status delivered, completed_at set", run.status == "completed"
                    and run.order_status == "delivered" and run.completed_at is not None and run.started_at is not None,
                    f"status={run.status} order_status={run.order_status}")
            events = (await session.execute(select(Event).where(Event.run_id == rid).order_by(Event.received_at, Event.id))).scalars().all()
            r.check("events: the four S1 events with their payloads", [ev.event_type for ev in events] == [*ROUTINE_EVENTS, "delivered"],
                    str([ev.event_type for ev in events]))
            actions = (await session.execute(select(Action).where(Action.run_id == rid))).scalars().all()
            r.check("actions: get_order_status + get_shipment_status, completed",
                    sorted((x.action_type, x.status) for x in actions) == [("get_order_status", "completed"), ("get_shipment_status", "completed")])
            executions = (await session.execute(select(ToolExecution).where(ToolExecution.action_id.in_([x.id for x in actions])))).scalars().all()
            r.check("tool_executions: both mocked tools ran through the Activity path and succeeded",
                    sorted((x.tool_name, x.status) for x in executions) == [("get_order_status", "success"), ("get_shipment_status", "success")])
            snaps = (await session.execute(select(MemorySnapshot).where(MemorySnapshot.run_id == rid))).scalars().all()
            r.check("memory_snapshots: 2", len(snaps) == 2)
            outputs = (await session.execute(select(FinalOutput).where(FinalOutput.run_id == rid))).scalars().all()
            r.check("final_outputs: 1 row, source llm (the FakeLLM's scripted report)", len(outputs) == 1 and outputs[0].output["source"] == "llm"
                    and outputs[0].output["summary"] == "Order delivered without any intervention.")
            timeline = (await session.execute(select(TimelineEntry).where(TimelineEntry.run_id == rid))).scalars().all()
            by_type: Dict[str, int] = {}
            for t in timeline:
                by_type[t.entry_type] = by_type.get(t.entry_type, 0) + 1
            r.check("timeline: 9 entries (4 event, 2 decision, 2 action, 1 system)", by_type == {"event": 4, "decision": 2, "action": 2, "system": 1}, str(by_type))
            completed_at = run.completed_at
            state["run"] = {"status": run.status, "order_status": run.order_status, "completed_at": run.completed_at}
        mock = await self.mock_state()
        (order,), (shipment,) = mock["orders"], mock["shipments"]
        r.check("mock world: order + shipment delivered, not escalated, no messages",
                (order["status"], shipment["status"], shipment["escalated"], shipment["delay_reason"], len(mock["messages"]))
                == ("delivered", "delivered", False, None, 0))
        close = (await self.handle.describe()).close_time
        if close is not None and completed_at is not None:
            r.check("runs.completed_at is not after the Temporal close time (complete_run commits first)",
                    norm(completed_at) <= norm(close) + timedelta(seconds=2), f"completed_at={completed_at} close={close}", kind="note")
        e.json("db_final_state.json", await self._dump_database(engine))

    async def _dump_database(self, engine) -> Dict[str, Any]:
        queries = {
            "supervisors": "select * from supervisors where name like :p",
            "runs": "select * from runs where order_id like :p",
            "events": "select e.* from events e join runs r on r.id=e.run_id where r.order_id like :p order by e.received_at",
            "timeline_entries": "select t.* from timeline_entries t join runs r on r.id=t.run_id where r.order_id like :p order by t.created_at",
            "actions": "select a.* from actions a join runs r on r.id=a.run_id where r.order_id like :p order by a.created_at",
            "tool_executions": "select x.* from tool_executions x join actions a on a.id=x.action_id join runs r on r.id=a.run_id where r.order_id like :p order by x.started_at",
            "memory_snapshots": "select m.* from memory_snapshots m join runs r on r.id=m.run_id where r.order_id like :p order by m.created_at",
            "final_outputs": "select f.* from final_outputs f join runs r on r.id=f.run_id where r.order_id like :p",
            "mock_orders": "select * from mock_orders where order_id like :p",
            "mock_shipments": "select * from mock_shipments where order_id like :p",
            "mock_customer_messages": "select * from mock_customer_messages where order_id like :p",
        }
        out: Dict[str, Any] = {}
        async with engine.connect() as conn:
            for name, sql in queries.items():
                out[name] = [dict(row) for row in (await conn.execute(text(sql), {"p": f"{PREFIX}%"})).mappings().all()]
        return out

    async def _check_observation(self) -> None:
        r = self.report
        say("\n== observation endpoints against the real run")
        get = lambda suffix: self.http.get(f"/api/runs/{self.run_id}{suffix}")  # noqa: E731
        run = (await get("")).json()
        r.check("GET /runs/{id}: completed / delivered", (run["status"], run["order_status"]) == ("completed", "delivered")
                and run["completed_at"] is not None)
        final = (await get("/final-output")).json()
        output = final["final_output"] or {}
        r.check("GET /final-output: five keys, source llm, scripted summary",
                set(output) == {"summary", "key_actions", "key_learnings", "recommendations", "source"}
                and output.get("source") == "llm" and output.get("summary") == "Order delivered without any intervention.")
        actions = (await get("/actions")).json()["actions"]
        r.check("GET /actions", [(a["action_type"], a["status"]) for a in actions] == [("get_order_status", "completed"), ("get_shipment_status", "completed")])
        tools = (await get("/tool-executions")).json()["tool_executions"]
        r.check("GET /tool-executions: results from the mock tables", [(t["tool_name"], t["status"], t["error"]) for t in tools]
                == [("get_order_status", "success", None), ("get_shipment_status", "success", None)]
                and tools[0]["result"] == {"order_id": ORDER_ID, "status": "created"}
                and tools[1]["result"]["shipment_id"] == shipment_id_for(ORDER_ID) and tools[1]["result"]["escalated"] is False)
        memory = (await get("/memory")).json()["snapshots"]
        r.check("GET /memory: two snapshots in order", [s["memory"]["situation_summary"] for s in memory]
                == ["Order placed; awaiting payment", "Payment confirmed and shipment created; no delay observed"])
        entries = (await get("/timeline")).json()["entries"]
        kinds: Dict[str, int] = {}
        for entry in entries:
            kinds[entry["entry_type"]] = kinds.get(entry["entry_type"], 0) + 1
        r.check("GET /timeline: 9 entries", kinds == {"event": 4, "decision": 2, "action": 2, "system": 1}, str(kinds))


# ================================================================ orchestration


class Components:
    def __init__(self, evidence: Evidence) -> None:
        self.evidence = evidence
        self.procs: Dict[str, subprocess.Popen] = {}
        self.env = dict(os.environ, PYTHONPATH=str(BACKEND), PYTHONUNBUFFERED="1")

    def spawn(self, name: str, argv: List[str]) -> subprocess.Popen:
        log = open(self.evidence.root / "logs" / f"{name}.log", "wb")
        proc = subprocess.Popen(argv, cwd=str(BACKEND), env=self.env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self.procs[name] = proc
        return proc

    def alive(self, name: str) -> bool:
        return self.procs[name].poll() is None

    def stop(self, name: str) -> str:
        proc = self.procs.get(name)
        if proc is None or proc.poll() is not None:
            return "not running"
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=25)
            return "stopped (SIGTERM)"
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
            return "killed (SIGKILL after 25s)"


def log_text(evidence: Evidence, name: str) -> str:
    path = evidence.root / "logs" / f"{name}.log"
    return path.read_text(errors="replace") if path.exists() else ""


async def start_components(comps: Components, args: argparse.Namespace, api_port: int) -> None:
    settings = get_settings()
    script = str(Path(__file__).resolve())
    say("starting Temporal dev server (first run downloads the dev-server binary; this can take a while) ...")
    comps.spawn("temporal", [sys.executable, script, "server"])

    async def server_up():
        if not comps.alive("temporal"):
            raise RuntimeError("Temporal dev server exited; see logs/temporal.log:\n" + log_text(comps.evidence, "temporal")[-1500:])
        with contextlib.suppress(Exception):
            await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
            return True
        return False

    await eventually(server_up, timeout=420, what="Temporal dev server accepting connections", interval=2)
    say("  Temporal server is up")
    comps.spawn("worker", [sys.executable, script, "worker", "--llm-log", str(comps.evidence.root / "llm_calls.jsonl")])
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)

    async def worker_polling():
        if not comps.alive("worker"):
            raise RuntimeError("worker exited; see logs/worker.log:\n" + log_text(comps.evidence, "worker")[-1500:])
        try:
            resp = await client.workflow_service.describe_task_queue(DescribeTaskQueueRequest(
                namespace=client.namespace, task_queue=TaskQueue(name=TASK_QUEUE), task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW))
            return len(resp.pollers) > 0
        except Exception:  # noqa: BLE001
            return False

    await eventually(worker_polling, timeout=90, what=f"worker polling task queue {TASK_QUEUE}", interval=1)
    say("  worker is polling")
    comps.spawn("api", [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(api_port), "--log-level", "info"])

    async def api_up():
        if not comps.alive("api"):
            raise RuntimeError("API exited; see logs/api.log:\n" + log_text(comps.evidence, "api")[-1500:])
        with contextlib.suppress(Exception):
            return httpx.get(f"http://127.0.0.1:{api_port}/health", timeout=3).status_code == 200
        return False

    await eventually(api_up, timeout=90, what="FastAPI /health", interval=1)
    say("  FastAPI is up")


def diagnostics(evidence: Evidence, comps: Optional[Components]) -> None:
    """Diagnostic evidence only (never an acceptance criterion)."""
    ports = get_settings()
    lsof = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN,ESTABLISHED"], capture_output=True, text=True).stdout
    keep = [l for l in lsof.splitlines() if any(p in l for p in (":7233", ":8233", f":{ports.port}", "COMMAND"))]
    evidence.text("diagnostics/lsof.txt", "\n".join(keep) + "\n")
    pids = ",".join(str(p.pid) for p in (comps.procs.values() if comps else []))
    ps = subprocess.run(["ps", "-o", "pid,ppid,etime,command", "-p", pids], capture_output=True, text=True).stdout if pids else ""
    evidence.text("diagnostics/processes.txt", ps)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_group(pid: int, grace: float = 25.0) -> str:
    """SIGTERM a component's process group, wait, then SIGKILL. Only ever touches our own processes."""
    if not pid_alive(pid):
        return "not running"
    command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    if "runtime_validation.py" not in command and "uvicorn app.main:app" not in command:
        return f"pid {pid} is not a Step 9 component (stale state?); left alone"
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.5)
    if pid_alive(pid):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)
        time.sleep(1)
        return f"killed (SIGKILL after {grace:.0f}s)"
    return "stopped (SIGTERM)"


async def stop_and_clean(evidence: Evidence, comps: Optional[Components], state: Dict[str, Any]) -> Dict[str, Any]:
    settings = get_settings()
    result: Dict[str, Any] = {"steps": {}}
    try:  # a workflow left running by a failed validation would keep the worker busy
        client = await asyncio.wait_for(Client.connect(settings.temporal_address, namespace=settings.temporal_namespace), 5)
        handle = client.get_workflow_handle(WORKFLOW_ID)
        if (await handle.describe()).status == WorkflowExecutionStatus.RUNNING:
            await handle.terminate(reason="Step 9A validation cleanup")
            result["steps"]["terminated_leftover_workflow"] = WORKFLOW_ID
    except Exception:  # noqa: BLE001 - no server, or no such workflow
        pass
    if comps is not None:
        for name in ("worker", "api", "temporal"):
            result["steps"][f"stop_{name}"] = comps.stop(name)
    else:  # standalone `cleanup` (e.g. after --keep): same order as the `all` path
        for name in ("worker", "api", "temporal"):
            pid = state.get("pids", {}).get(name)
            if pid:
                result["steps"][f"stop_{name}"] = stop_group(pid)
    ports = state.get("ports", {})
    await asyncio.sleep(1)
    released = {name: not port_open("127.0.0.1", p) for name, p in ports.items()}
    result["ports_released"] = released
    leftovers = pgrep(r"runtime_validation\.py (server|worker)") + pgrep(r"uvicorn app\.main:app")
    result["leftover_step9_processes"] = leftovers
    engine = make_engine()
    try:
        await cleanup_database(engine)
        after = await table_counts(engine)
        before = state.get("table_counts_before") or {}
        result["table_counts_after"] = after
        result["tables_match_pre_test_state"] = (after == before) if before else None
        result["prefix_rows_after"] = await prefix_rows(engine)
        alembic = await alembic_state(engine)
        result["alembic"] = alembic
    finally:
        await engine.dispose()
    result["ok"] = (all(released.values()) and not leftovers and result.get("tables_match_pre_test_state") is True
                    and not any(result["prefix_rows_after"].values()) and result["alembic"]["current"] == result["alembic"]["head"])
    evidence.json("cleanup.json", result)
    return result


def secret_scan(evidence: Evidence) -> List[str]:
    """Evidence must not contain the database URL/password or any sk- style key."""
    url = get_settings().database_url
    needles = [url]
    password = make_url(url).password
    if password:
        needles.append(f":{password}@")
    findings = []
    for path in evidence.root.rglob("*"):
        if path.is_file():
            content = path.read_text(errors="replace")
            if any(n in content for n in needles) or re.search(r"sk-[A-Za-z0-9_-]{16,}", content):
                findings.append(str(path.relative_to(evidence.root)))
    return findings


def write_summary(evidence: Evidence, report: Report, info: Dict[str, Any], timing: Dict[str, Any], exit_code: int) -> None:
    lines = ["# Step 9A - real Temporal runtime validation", "",
             f"Result: **{'PASS' if exit_code == 0 else 'FAIL'}** ({len(report.failures)} hard failures, {len(report.notes)} notes)", "",
             "| Status | Check | Detail |", "|---|---|---|"]
    for row in report.rows:
        lines.append(f"| {row['status']} | {row['check']} | {str(row['detail']).replace('|', '/')[:220]} |")
    timing_view = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in timing.items() if k != "history"}
    lines += ["", "## Timing", "```", json.dumps(timing_view, indent=2, default=str), "```", "", "## Versions / state", "```",
              json.dumps(info, indent=2, default=str), "```"]
    evidence.text("summary.md", "\n".join(lines) + "\n")
    evidence.json("report.json", {"exit_code": exit_code, "checks": report.rows})


def collect_versions(evidence: Evidence, pre: Dict[str, Any]) -> Dict[str, Any]:
    def sh(*cmd: str) -> str:
        return subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT)).stdout.strip()

    info = dict(pre["info"])
    info.update(commit=sh("git", "rev-parse", "HEAD"), git_status_before=sh("git", "status", "--short"),
                platform=sys.platform, versions={n: importlib.metadata.version(n) for n in
                                                 ("temporalio", "fastapi", "uvicorn", "httpx", "sqlalchemy", "asyncpg", "pydantic")})
    evidence.json("versions.json", info)
    return info


# ==================================================================== commands


async def cmd_preflight(args: argparse.Namespace) -> int:
    result = await preflight(expect_components_running=False)
    say(json.dumps(result["info"], indent=2, default=str))
    for problem in result["problems"]:
        say(f"PROBLEM: {problem}")
    say("preflight: " + ("OK" if not result["problems"] else "FAILED"))
    return 0 if not result["problems"] else 2


async def cmd_scenario(args: argparse.Namespace) -> int:
    root = Path(args.evidence_dir).expanduser() / stamp() if args.evidence_dir else DEFAULT_EVIDENCE_ROOT / stamp()
    evidence = Evidence(root)
    report = Report()
    pre = await preflight(expect_components_running=True)
    if pre["problems"]:
        for p in pre["problems"]:
            say(f"PROBLEM: {p}")
        return 2
    evidence.json("db_counts_before.json", pre["info"]["table_counts_before"])
    scenario = Scenario(args, evidence, report)
    try:
        await scenario.run()
    except Exception as err:  # noqa: BLE001
        report.check("scenario completed without an unexpected error", False, f"{type(err).__name__}: {err}")
        evidence.text("scenario_error.txt", traceback.format_exc())
    exit_code = 1 if report.failures else 0
    write_summary(evidence, report, pre["info"], scenario.timing, exit_code)
    say(f"\nevidence: {evidence.root}")
    return exit_code


async def cmd_all(args: argparse.Namespace) -> int:
    root = (Path(args.evidence_dir).expanduser() if args.evidence_dir else DEFAULT_EVIDENCE_ROOT) / stamp()
    evidence = Evidence(root)
    report = Report()
    say(f"evidence directory: {root}")
    pre = await preflight(expect_components_running=False)
    if pre["problems"]:
        for p in pre["problems"]:
            say(f"PROBLEM: {p}")
        evidence.json("preflight_failed.json", pre)
        return 2
    info = collect_versions(evidence, pre)
    api_port = args.api_port or get_settings().port
    state = {"table_counts_before": pre["info"]["table_counts_before"], "ports": {"temporal": pre["info"]["ports"]["temporal"],
             "ui": pre["info"]["ports"]["ui"], "api": api_port}, "pids": {}}
    evidence.json("db_counts_before.json", state["table_counts_before"])
    comps = Components(evidence)
    scenario = Scenario(argparse.Namespace(api_port=api_port, llm_log=str(evidence.root / "llm_calls.jsonl")), evidence, report)
    exit_code = 0
    try:
        await start_components(comps, args, api_port)
        state["pids"] = {n: p.pid for n, p in comps.procs.items()}
        evidence.json("state.json", state)
        report.check("real Temporal dev server started", comps.alive("temporal"), f"pid {comps.procs['temporal'].pid}")
        report.check("real worker started (production create_worker + FakeLLMClient)", comps.alive("worker"), f"pid {comps.procs['worker'].pid}")
        report.check("real FastAPI (uvicorn) started", comps.alive("api"), f"pid {comps.procs['api'].pid}")
        report.check("FastAPI connected to Temporal at startup (no 'Temporal unavailable' warning)",
                     "Temporal unavailable" not in log_text(evidence, "api"))
        await scenario.run()
        diagnostics(evidence, comps)
    except Exception as err:  # noqa: BLE001
        report.check("validation completed without an unexpected error", False, f"{type(err).__name__}: {err}")
        evidence.text("scenario_error.txt", traceback.format_exc())
    finally:
        if args.keep:
            say("--keep: components and API-TEST-S9-* rows left in place; run `cleanup` when finished")
            evidence.json("state.json", state)
        else:
            say("\n== cleanup")
            cleanup = await stop_and_clean(evidence, comps, state)
            report.check("cleanup: processes stopped, ports released, DB back to its pre-test state, Alembic unchanged",
                         cleanup["ok"], json.dumps({k: cleanup[k] for k in ("ports_released", "leftover_step9_processes", "tables_match_pre_test_state", "alembic")}, default=str))
            if not cleanup["ok"]:
                exit_code = 3
    findings = secret_scan(evidence)
    report.check("evidence contains no database URL/password or API-key-like strings", not findings, ", ".join(findings) or "clean")
    if exit_code == 0 and report.failures:
        exit_code = 1
    write_summary(evidence, report, info, scenario.timing, exit_code)
    say(f"\nRESULT: {'PASS' if exit_code == 0 else 'FAIL'}  ({len(report.failures)} hard failures, {len(report.notes)} notes)")
    for row in report.failures + report.notes:
        say(f"  [{row['status']}] {row['check']} -- {row['detail']}")
    say(f"evidence: {evidence.root}")
    return exit_code


async def cmd_cleanup(args: argparse.Namespace) -> int:
    if args.evidence_dir:
        root = Path(args.evidence_dir).expanduser()
    else:
        runs = sorted(p for p in DEFAULT_EVIDENCE_ROOT.glob("*") if (p / "state.json").exists())
        if not runs:
            say("no evidence directory with state.json found; cleaning the database namespace only")
            root = DEFAULT_EVIDENCE_ROOT / f"cleanup-{stamp()}"
        else:
            root = runs[-1]
    evidence = Evidence(root)
    state = json.loads((root / "state.json").read_text()) if (root / "state.json").exists() else {}
    result = await stop_and_clean(evidence, None, state)
    say(json.dumps(result, indent=2, default=str))
    return 0 if result["ok"] else 3


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight", help="read-only environment checks")
    sub.add_parser("server", help="run the Temporal dev server in the foreground")
    p = sub.add_parser("worker", help="run the worker (FakeLLMClient) in the foreground")
    p.add_argument("--llm-log", required=True)
    for name in ("scenario", "all"):
        p = sub.add_parser(name)
        p.add_argument("--evidence-dir", default=None)
        p.add_argument("--api-port", type=int, default=None)
        if name == "scenario":
            p.add_argument("--llm-log", required=True)
        else:
            p.add_argument("--keep", action="store_true", help="leave components and rows in place for a demo")
    p = sub.add_parser("cleanup")
    p.add_argument("--evidence-dir", default=None)
    args = parser.parse_args()
    handler = {"preflight": cmd_preflight, "server": cmd_server, "worker": cmd_worker, "scenario": cmd_scenario,
               "all": cmd_all, "cleanup": cmd_cleanup}[args.command]
    return asyncio.run(handler(args))


if __name__ == "__main__":
    sys.exit(main())
