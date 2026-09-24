"""Real Temporal + real LLM end-to-end scenario (Gemini). Manual tooling; NOT part of pytest.

Runs the production stack as separate processes and drives one small order lifecycle:

    driver -> FastAPI (uvicorn) -> real Temporal dev server <- the PRODUCTION worker
              (`python -m app.temporal.worker`, LLM chosen by LLM_PROVIDER, NOT a FakeLLMClient)
              -> Gemini (gemini-3.6-flash, reasoning_effort low, OpenAI-compatible endpoint)

Scenario (about 4 LLM calls, about 3 minutes; one run is enough, quota is limited):
  start -> reasoning #1 (workflow_start) -> payment_confirmed, shipment_created (routine, no LLM)
  -> shipment_delayed (important) -> reasoning #2 -> durable timer (max wake 1 minute) -> reasoning #3
  -> delivered -> final output (LLM #4) -> workflow COMPLETED -> PostgreSQL rows.

    .venv/bin/python backend/scripts/gemini_e2e.py             # REAL Gemini (LLM_PROVIDER=gemini + GEMINI_API_KEY)
    .venv/bin/python backend/scripts/gemini_e2e.py --mock-llm  # rehearsal against a local OpenAI-compatible stub

--mock-llm points the production worker at a local stub through LLM_BASE_URL. It proves the whole pipeline and
the exact HTTP request shape without spending quota; it does NOT prove Gemini itself.
Evidence goes OUTSIDE the repository; the API key is never printed or written.
Exit codes: 0 pass, 1 a hard check failed, 2 not configured / preflight problem, 3 cleanup failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import threading
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_validation as rv  # noqa: E402  (also puts backend/ on sys.path and sets the cwd)

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402
from temporalio.api.enums.v1 import TaskQueueType  # noqa: E402
from temporalio.api.taskqueue.v1 import TaskQueue  # noqa: E402
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest  # noqa: E402
from temporalio.client import Client, WorkflowExecutionStatus  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.mock_models import MockShipment  # noqa: E402
from app.db.models import Action, Event, FinalOutput, MemorySnapshot, Run, TimelineEntry, ToolExecution  # noqa: E402
from app.llm.client import GEMINI_BASE_URL, GEMINI_MODEL, GEMINI_REASONING_EFFORT  # noqa: E402
from app.llm.fake import make_decision_json, make_final_output_json  # noqa: E402
from app.llm.schemas import FINAL_OUTPUT_SCHEMA_NAME, REASONING_SCHEMA_NAME  # noqa: E402
from app.simulation.s1_smooth_delivery import s1_supervisor_body  # noqa: E402
from app.simulation.world import ExternalWorld  # noqa: E402
from app.temporal.constants import TASK_QUEUE, order_workflow_id  # noqa: E402
from app.temporal.workflows import OrderWorkflow  # noqa: E402

ORDER_ID = f"{rv.PREFIX}G01"
WORKFLOW_ID = order_workflow_id(ORDER_ID)
SUPERVISOR_NAME = f"{rv.PREFIX}Gemini E2E"
MOCK_KEY = "mock-key-not-a-real-credential"  # only ever used against the local stub
INSTRUCTIONS = (
    "Monitor the order until it is delivered. When you wake at workflow start, check the order with "
    "get_order_status. If a shipment is delayed, escalate it with escalate_shipment (a short reason and "
    "priority high). On scheduled reviews, check the shipment with get_shipment_status."
)
RUN_INSTRUCTION = "If the shipment is delayed, escalate immediately."
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"


# ============================================================== local stub


class MockLLM:
    """A local OpenAI-compatible Chat Completions stub that also verifies each request's shape."""

    def __init__(self, evidence: rv.Evidence) -> None:
        self.evidence = evidence
        self.requests: List[Dict[str, Any]] = []
        self.problems: List[str] = []
        self._lock = threading.Lock()
        outer = self
        decisions = [
            make_decision_json(assessment="Order just placed; checking its state.", tool="get_order_status",
                               situation_summary="Order placed; awaiting payment", next_wake_in_minutes=60),
            make_decision_json(assessment="Shipment delayed; escalating.", tool="escalate_shipment",
                               reason="Carrier capacity shortage", priority="high",
                               situation_summary="Shipment delayed and escalated", next_wake_in_minutes=60),
            make_decision_json(assessment="Scheduled review of the shipment.", tool="get_shipment_status",
                               situation_summary="Delay escalated; awaiting delivery", next_wake_in_minutes=60),
        ]

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # silence
                pass

            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
                with outer._lock:
                    problems = outer._check(self.path, self.headers.get("authorization", ""), body)
                    name = (body.get("response_format", {}).get("json_schema", {}) or {}).get("name")
                    reasoning_calls = sum(1 for r in outer.requests if r["schema"] == REASONING_SCHEMA_NAME)
                    outer.requests.append({"path": self.path, "schema": name, "problems": problems})
                    outer.problems.extend(problems)
                    outer.evidence.jsonl("mock_llm_requests.jsonl", {"path": self.path, "schema": name,
                                                                    "model": body.get("model"),
                                                                    "reasoning_effort": body.get("reasoning_effort"),
                                                                    "body_keys": sorted(body), "problems": problems})
                if name == FINAL_OUTPUT_SCHEMA_NAME:
                    content = make_final_output_json(
                        summary="Order delivered after one escalated carrier delay.",
                        key_actions=["Checked the order", "Escalated the delayed shipment", "Reviewed the shipment"],
                        key_learnings=["Escalating a delay immediately kept the delivery on track"],
                        recommendations=["Keep escalating carrier delays at once"])
                else:
                    content = decisions[min(reasoning_calls, len(decisions) - 1)]
                payload = json.dumps({"id": "chatcmpl-mock", "object": "chat.completion", "created": 1,
                                      "model": body.get("model"), "choices": [{"index": 0, "finish_reason": "stop",
                                      "message": {"role": "assistant", "content": content}}]}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @staticmethod
    def _check(path: str, authorization: str, body: Dict[str, Any]) -> List[str]:
        problems = []
        if not path.endswith("/v1beta/openai/chat/completions"):
            problems.append(f"unexpected path {path}")
        if not authorization.startswith("Bearer "):
            problems.append("missing bearer authorization")
        if set(body) != {"model", "messages", "response_format", "reasoning_effort"}:
            problems.append(f"unexpected body keys {sorted(body)}")
        if body.get("model") != GEMINI_MODEL:
            problems.append(f"model {body.get('model')!r}")
        if body.get("reasoning_effort") != GEMINI_REASONING_EFFORT:
            problems.append(f"reasoning_effort {body.get('reasoning_effort')!r}")
        fmt = body.get("response_format", {})
        schema = fmt.get("json_schema", {})
        if fmt.get("type") != "json_schema" or schema.get("strict") is not True or not schema.get("schema"):
            problems.append("response_format is not a strict json_schema")
        return problems

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


# ================================================================ scenario


def gemini_key() -> str:
    key = get_settings().gemini_api_key
    return key.get_secret_value().strip() if key is not None else ""


async def start_components(comps: rv.Components, api_port: int) -> None:
    settings = get_settings()
    script = str(Path(rv.__file__).resolve())
    say = rv.say
    say("starting Temporal dev server ...")
    comps.spawn("temporal", [sys.executable, script, "server"])

    async def server_up():
        if not comps.alive("temporal"):
            raise RuntimeError("Temporal dev server exited:\n" + rv.log_text(comps.evidence, "temporal")[-1500:])
        try:
            await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
            return True
        except Exception:  # noqa: BLE001
            return False

    await rv.eventually(server_up, timeout=420, what="Temporal server", interval=2)
    say("starting the PRODUCTION worker (python -m app.temporal.worker) ...")
    comps.spawn("worker", [sys.executable, "-m", "app.temporal.worker"])
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)

    async def worker_polling():
        if not comps.alive("worker"):
            raise RuntimeError("worker exited:\n" + rv.log_text(comps.evidence, "worker")[-1500:])
        try:
            resp = await client.workflow_service.describe_task_queue(DescribeTaskQueueRequest(
                namespace=client.namespace, task_queue=TaskQueue(name=TASK_QUEUE),
                task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW))
            return len(resp.pollers) > 0
        except Exception:  # noqa: BLE001
            return False

    await rv.eventually(worker_polling, timeout=90, what="worker polling", interval=1)
    say("starting FastAPI ...")
    comps.spawn("api", [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(api_port),
                        "--log-level", "info"])

    async def api_up():
        if not comps.alive("api"):
            raise RuntimeError("API exited:\n" + rv.log_text(comps.evidence, "api")[-1500:])
        try:
            return httpx.get(f"http://127.0.0.1:{api_port}/health", timeout=3).status_code == 200
        except Exception:  # noqa: BLE001
            return False

    await rv.eventually(api_up, timeout=90, what="FastAPI", interval=1)


class E2E:
    def __init__(self, evidence: rv.Evidence, report: rv.Report, api_port: int, mock: Optional[MockLLM]) -> None:
        self.evidence, self.report, self.api_port, self.mock = evidence, report, api_port, mock
        self.timing: Dict[str, Any] = {}
        self.run_id = ""

    async def status(self) -> Optional[Dict[str, Any]]:
        r = await self.http.get(f"/api/runs/{self.run_id}/status")
        return r.json() if r.status_code == 200 else None

    async def settled(self, cycles: int, timeout: float, what: str) -> Dict[str, Any]:
        async def check():
            s = await self.status()
            return s if s and s["reasoning_count"] == cycles and s["state"] == "sleeping" else None

        try:
            return await rv.eventually(check, timeout=timeout, what=what, interval=0.5)
        except TimeoutError:
            await self._explain_failure()
            raise

    async def _explain_failure(self) -> None:
        """Surface the workflow's own (already sanitised) failure notes: they say WHY a cycle failed."""
        s = await self.status()
        entries = (await self.http.get(f"/api/runs/{self.run_id}/timeline")).json().get("entries", [])
        notes = [e["message"] for e in entries if e["entry_type"] == "system"]
        rv.say(f"    status: state={s and s['state']} cycles={s and s['reasoning_count']} "
               f"last_cycle_outcome={s and s['last_cycle_outcome']}")
        for note in notes[-3:]:
            rv.say(f"    system note: {note[:300]}")

    async def recorded(self, event_type: str) -> None:
        expected = f"Event received: {event_type}"

        async def check():
            entries = (await self.http.get(f"/api/runs/{self.run_id}/timeline")).json()["entries"]
            return any(e["message"].startswith(expected) for e in entries)

        await rv.eventually(check, timeout=30, what=f"workflow recorded {event_type}")

    async def run(self, engine) -> None:
        r, e = self.report, self.evidence
        settings = get_settings()
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
        self.http = httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.api_port}", timeout=60)
        world = ExternalWorld(factory, self.http)
        try:
            rv.say("\n== supervisor, order and run (through the real API)")
            body = s1_supervisor_body(SUPERVISOR_NAME)
            body.update(instructions=INSTRUCTIONS, default_wake_interval_minutes=1, max_wake_interval_minutes=1)
            created = await self.http.post("/api/supervisors", json=body)
            r.check("POST /api/supervisors -> 201", created.status_code == 201, created.text[:120])
            await world.place_order(ORDER_ID)
            made = await self.http.post("/api/runs", json={"order_id": ORDER_ID, "supervisor_id": created.json()["id"],
                                                           "run_instructions": [RUN_INSTRUCTION]})
            r.check("POST /api/runs -> 201 running", made.status_code == 201 and made.json()["status"] == "running",
                    f"http={made.status_code}")
            self.run_id = made.json()["id"]
            self.timing["run_created_at"] = rv.now_utc()
            handle = client.get_workflow_handle_for(OrderWorkflow.run, WORKFLOW_ID)

            rv.say("\n== reasoning #1 (workflow_start) -> LLM")
            s1 = await self.settled(1, 120, "reasoning #1 (LLM call 1)")
            r.check("reasoning #1 completed via the LLM (no failure)", s1["last_cycle_outcome"] == "completed"
                    and s1["last_wake_reason"] == "workflow_start", f"outcome={s1['last_cycle_outcome']}")

            rv.say("\n== routine events (no LLM), then an important event (LLM)")
            await world.confirm_payment(self.run_id, ORDER_ID)
            await self.recorded("payment_confirmed")
            await world.create_shipment(self.run_id, ORDER_ID)
            await self.recorded("shipment_created")
            s = await self.status()
            r.check("routine events did not wake the LLM", s is not None and s["reasoning_count"] == 1)
            await world.delay_shipment(self.run_id, ORDER_ID, "Carrier capacity shortage")
            s2 = await self.settled(2, 120, "reasoning #2 (LLM call 2)")
            r.check("reasoning #2 was woken by the important event and completed",
                    s2["last_wake_reason"] == "important_event" and s2["last_cycle_outcome"] == "completed",
                    f"reason={s2['last_wake_reason']} outcome={s2['last_cycle_outcome']}")

            rv.say("\n== durable timer -> reasoning #3 (LLM call 3); this waits up to a minute")
            s3 = await self.settled(3, 200, "reasoning #3 (timer-driven, LLM call 3)")
            r.check("reasoning #3 was timer-driven and completed", s3["last_wake_reason"] == "scheduled_wakeup"
                    and s3["last_cycle_outcome"] == "completed", f"reason={s3['last_wake_reason']}")

            rv.say("\n== delivered -> terminal -> final output (LLM call 4)")
            await world.deliver(self.run_id, ORDER_ID)
            result = await asyncio.wait_for(handle.result(), timeout=180)
            desc = await handle.describe()
            e.json("workflow_describe.json", {"workflow_id": WORKFLOW_ID, "status": desc.status.name,
                                             "history_length": desc.history_length, "close_time": desc.close_time})
            r.check("Temporal execution reached COMPLETED (independent client)", desc.status == WorkflowExecutionStatus.COMPLETED,
                    f"status={desc.status.name} history_length={desc.history_length}")
            r.check("workflow result: 3 reasoning cycles, 4 events, terminal, final output persisted",
                    (result.reasoning_count, result.events_received, result.state, result.final_output_persisted)
                    == (3, 4, "terminal", True), f"{result.reasoning_count=} {result.events_received=} {result.state=}")

            analysis = rv.analyse_history(list((await handle.fetch_history()).events))
            e.text("temporal_history_table.txt", rv.history_table(analysis))
            done, failed = analysis["activities_completed"], analysis["activities_failed"]
            r.check("3 reasoning Activities and 1 final-output Activity completed on the worker",
                    done.get("generate_reasoning_decision") == 3 and done.get("generate_final_output") == 1, str(done))
            r.check("reasoning #3 was triggered by a TimerFired event", (rv.wake_trigger_of_reasoning(analysis, 2) or {}).get("type") == "TIMER_FIRED")
            r.check("no Activity failed; none needed a retry", not failed and analysis["activity_max_attempt"] <= 1,
                    f"failed={failed} max_attempt={analysis['activity_max_attempt']}", kind="note")
            r.check("no workflow task failures", analysis["workflow_task_failed"] == 0)

            await self._check_database(engine, factory)
            await self._check_llm_traffic()
            e.json("timing.json", {k: v for k, v in self.timing.items()})
        finally:
            await self.http.aclose()

    async def _check_database(self, engine, factory) -> None:
        r, e = self.report, self.evidence
        import uuid

        rid = uuid.UUID(self.run_id)
        rv.say("\n== PostgreSQL")
        async with factory() as session:
            run = await session.get(Run, rid)
            r.check("runs: completed / delivered", (run.status, run.order_status) == ("completed", "delivered")
                    and run.completed_at is not None, f"{run.status}/{run.order_status}")
            events = (await session.execute(select(Event).where(Event.run_id == rid).order_by(Event.received_at))).scalars().all()
            r.check("events: the four events were recorded", [x.event_type for x in events]
                    == ["payment_confirmed", "shipment_created", "shipment_delayed", "delivered"])
            snaps = (await session.execute(select(MemorySnapshot).where(MemorySnapshot.run_id == rid))).scalars().all()
            r.check("memory_snapshots: 3 (one per completed reasoning cycle)", len(snaps) == 3, str(len(snaps)))
            timeline = (await session.execute(select(TimelineEntry).where(TimelineEntry.run_id == rid))).scalars().all()
            decisions = [t for t in timeline if t.entry_type == "decision"]
            failures = [t.message[:200] for t in timeline if t.entry_type == "system" and "failed" in t.message.lower()]
            r.check("timeline: 3 decision entries, no LLM failure notes", len(decisions) == 3 and not failures,
                    f"decisions={len(decisions)} failures={failures}")
            actions = (await session.execute(select(Action).where(Action.run_id == rid))).scalars().all()
            executions = (await session.execute(select(ToolExecution).where(ToolExecution.action_id.in_([a.id for a in actions])))).scalars().all()
            r.check("mock tools ran through the Activity path (LLM-chosen), all succeeded",
                    len(executions) >= 1 and all(x.status == "success" for x in executions),
                    str(sorted((x.tool_name, x.status) for x in executions)))
            out = (await session.execute(select(FinalOutput).where(FinalOutput.run_id == rid))).scalars().all()
            output = out[0].output if out else {}
            r.check("final_outputs: 1 row, source 'llm' (not the deterministic fallback), non-empty summary",
                    len(out) == 1 and output.get("source") == "llm" and bool(output.get("summary")),
                    f"source={output.get('source')!r} keys={sorted(output)}")
            e.json("decisions_and_memory.json", {
                "decisions": [d.message for d in decisions],
                "memory": [s.memory for s in sorted(snaps, key=lambda s: s.created_at)],
                "tools": [(x.tool_name, x.status, x.result) for x in executions], "final_output": output})
            shipment = (await session.execute(select(MockShipment).where(MockShipment.order_id == ORDER_ID))).scalar_one()
            r.check("mock world ended delivered; the escalation (if the LLM chose it) is recorded", shipment.status == "delivered",
                    f"escalated={shipment.escalated}")
        e.json("db_final_state.json", await rv.Scenario._dump_database(None, engine))

    async def _check_llm_traffic(self) -> None:
        r, e = self.report, self.evidence
        worker_log = rv.log_text(e, "worker")
        ok_lines = [l for l in worker_log.splitlines() if "HTTP Request: POST" in l and "/chat/completions" in l and '200 OK' in l]
        bad_lines = [l for l in worker_log.splitlines() if "HTTP Request: POST" in l and "/chat/completions" in l and '200 OK' not in l]
        expected_url = GEMINI_URL if self.mock is None else f"http://127.0.0.1:{self.mock.port}/v1beta/openai/chat/completions"
        e.text("llm_http_calls.txt", "\n".join(ok_lines + bad_lines) + "\n")
        rv.say("\n== LLM traffic (the worker's SDK/httpx log lines: URL and status only)")
        r.check(f"the worker made 4 successful chat/completions calls to {'the local stub' if self.mock else 'Gemini'}",
                len(ok_lines) == 4 and all(expected_url in l for l in ok_lines), f"200 OK lines={len(ok_lines)} non-200={len(bad_lines)}")
        r.check("no failed LLM HTTP calls", not bad_lines, f"{len(bad_lines)}", kind="note")
        if self.mock is not None:
            r.check("the stub saw 4 requests, all with the exact approved shape (model, low effort, strict schema)",
                    len(self.mock.requests) == 4 and not self.mock.problems, f"requests={len(self.mock.requests)} problems={self.mock.problems}")
            r.check("stub saw reasoning x3 then final_output", [q["schema"] for q in self.mock.requests]
                    == [REASONING_SCHEMA_NAME] * 3 + [FINAL_OUTPUT_SCHEMA_NAME])


def scan_for_key(evidence: rv.Evidence, keys: List[str]) -> List[str]:
    findings = []
    for path in evidence.root.rglob("*"):
        if path.is_file():
            content = path.read_text(errors="replace")
            if any(k and k in content for k in keys) or re.search(r"AIza[0-9A-Za-z_-]{20,}|sk-[A-Za-z0-9_-]{16,}", content):
                findings.append(str(path.relative_to(evidence.root)))
    return findings


async def amain(args: argparse.Namespace) -> int:
    settings = get_settings()
    mock_mode = args.mock_llm
    if not mock_mode:
        if settings.llm_provider != "gemini" or not gemini_key():
            print("NOT CONFIGURED: set LLM_PROVIDER=gemini and GEMINI_API_KEY in the git-ignored backend/.env. "
                  "No request was made. (Use --mock-llm for a no-quota rehearsal.)")
            return 2
    root = Path(args.evidence_dir).expanduser() if args.evidence_dir else rv.DEFAULT_EVIDENCE_ROOT / "gemini-e2e"
    evidence = rv.Evidence(root / rv.stamp())
    report = rv.Report()
    rv.say(f"mode: {'MOCK stub (rehearsal, no Gemini calls)' if mock_mode else 'REAL Gemini'} | evidence: {evidence.root}")
    pre = await rv.preflight(expect_components_running=False)
    if pre["problems"]:
        for p in pre["problems"]:
            rv.say(f"PROBLEM: {p}")
        return 2
    info = rv.collect_versions(evidence, pre)
    info["llm"] = {"mode": "mock" if mock_mode else "gemini", "model": settings.llm_model or GEMINI_MODEL,
                   "endpoint": settings.llm_base_url or GEMINI_BASE_URL, "reasoning_effort": settings.llm_reasoning_effort or GEMINI_REASONING_EFFORT}
    api_port = settings.port
    state = {"table_counts_before": pre["info"]["table_counts_before"],
             "ports": {"temporal": pre["info"]["ports"]["temporal"], "ui": pre["info"]["ports"]["ui"], "api": api_port}, "pids": {}}
    rv.WORKFLOW_ID = WORKFLOW_ID  # so cleanup terminates THIS scenario's workflow if it is left running
    comps = rv.Components(evidence)
    comps.env["OPENAI_LOG"] = "info"  # SDK/httpx log "POST <url> 200 OK" lines (never the key) into the worker log
    mock: Optional[MockLLM] = None
    if mock_mode:
        mock = MockLLM(evidence)
        mock.start()
        comps.env.update(LLM_PROVIDER="gemini", GEMINI_API_KEY=MOCK_KEY,
                         LLM_BASE_URL=f"http://127.0.0.1:{mock.port}/v1beta/openai/")
    exit_code = 0
    e2e = E2E(evidence, report, api_port, mock)
    engine = rv.make_engine()
    try:
        await start_components(comps, api_port)
        state["pids"] = {n: p.pid for n, p in comps.procs.items()}
        evidence.json("state.json", state)
        report.check("production worker started (python -m app.temporal.worker; LLM from LLM_PROVIDER, not injected)", comps.alive("worker"))
        await e2e.run(engine)
        rv.diagnostics(evidence, comps)
    except Exception as err:  # noqa: BLE001
        report.check("scenario completed without an unexpected error", False, f"{type(err).__name__}: {str(err)[:300]}")
        evidence.text("scenario_error.txt", traceback.format_exc())
    finally:
        await engine.dispose()
        rv.say("\n== cleanup")
        cleanup = await rv.stop_and_clean(evidence, comps, state)
        if mock is not None:
            mock.stop()
        report.check("cleanup: processes stopped, ports released, DB back to its pre-test state, Alembic unchanged",
                     cleanup["ok"], json.dumps({k: cleanup[k] for k in ("ports_released", "leftover_step9_processes", "tables_match_pre_test_state")}, default=str))
        if not cleanup["ok"]:
            exit_code = 3
    findings = scan_for_key(evidence, [gemini_key(), MOCK_KEY] if not mock_mode else [MOCK_KEY])
    report.check("evidence contains no API key material", not findings, ", ".join(findings) or "clean")
    if exit_code == 0 and report.failures:
        exit_code = 1
    rv.write_summary(evidence, report, info, e2e.timing, exit_code)
    rv.say(f"\nRESULT: {'PASS' if exit_code == 0 else 'FAIL'}  ({len(report.failures)} hard failures, {len(report.notes)} notes)")
    for row in report.failures + report.notes:
        rv.say(f"  [{row['status']}] {row['check']} -- {row['detail']}")
    rv.say(f"evidence: {evidence.root}")
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mock-llm", action="store_true", help="rehearse against a local OpenAI-compatible stub (no Gemini calls)")
    parser.add_argument("--evidence-dir", default=None)
    return asyncio.run(amain(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
