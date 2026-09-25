"""Manual live check of the real LLM provider: Gemini. NOT part of the pytest suite.

Sends one real reasoning request and one real final-output request through the runtime's own
LLM client (the OpenAI SDK against Google's OpenAI-compatible endpoint, gemini-3.1-flash-lite by default,
reasoning_effort only if LLM_REASONING_EFFORT is set) with the application's own prompts and strict schemas, and reports
whether the answers pass the same local validation the workflow uses. It uses 2 Gemini calls.

Needs LLM_PROVIDER=gemini and GEMINI_API_KEY (git-ignored backend/.env or the environment).
Only safe information is printed: never the key.

    .venv/bin/python backend/scripts/llm_smoke.py

Exit code: 0 both checks passed, 1 a check failed, 2 not configured (no request is made).
"""

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
os.chdir(BACKEND)  # Settings look for .env / backend/.env relative to the working directory
sys.path.insert(0, str(BACKEND))

from app.config import get_settings
from app.llm.client import GEMINI_BASE_URL, GEMINI_MODEL, GEMINI_REASONING_EFFORT, LLMError, OpenAILLMClient
from app.llm.prompts import (
    FINAL_OUTPUT_SYSTEM_PROMPT,
    REASONING_SYSTEM_PROMPT,
    build_final_output_user_prompt,
    build_reasoning_user_prompt,
)
from app.llm.schemas import (
    FINAL_OUTPUT_SCHEMA_NAME,
    REASONING_SCHEMA_NAME,
    InvalidLLMOutputError,
    final_output_json_schema,
    parse_final_output,
    parse_reasoning_decision,
    reasoning_decision_json_schema,
)
from app.temporal.contracts import FinalOutputInput, ReasoningInput
from app.temporal.types import OrderEvent

REASONING_CONTEXT = ReasoningInput(
    order_id="SMOKE-1",
    order_status="delayed",
    wake_reason="important_event",
    supervisor_instructions="Keep the customer informed and escalate serious delays.",
    supervisor_version=1,
    run_instructions=["If the shipment is delayed, escalate immediately."],
    enabled_tools=["get_order_status", "get_shipment_status", "escalate_shipment", "send_customer_update"],
    memory={"situation_summary": "Order shipped yesterday.", "open_concerns": []},
    new_events=[OrderEvent(event_type="shipment_delayed", payload={"delay_reason": "Carrier capacity shortage"})],
    recent_events=[OrderEvent(event_type="shipment_created")],
    last_action_result=None,
    default_wake_interval_minutes=60,
    min_wake_interval_minutes=1,
    max_wake_interval_minutes=1440,
    now=datetime.now(timezone.utc),
)
FINAL_CONTEXT = FinalOutputInput(
    order_id="SMOKE-1",
    final_order_status="delivered",
    supervisor_instructions="Keep the customer informed and escalate serious delays.",
    run_instructions=[],
    memory={"situation_summary": "Shipment was delayed, escalated, then delivered.", "open_concerns": []},
    recent_events=[OrderEvent(event_type="shipment_delayed"), OrderEvent(event_type="delivered")],
    action_log=[{"tool": "escalate_shipment", "success": True, "output": {}, "error": None}],
    events_received=3,
    reasoning_count=2,
)


async def main() -> int:
    settings = get_settings()
    if settings.llm_provider != "gemini":
        print(f"NOT CONFIGURED: LLM_PROVIDER is {settings.llm_provider!r}; set LLM_PROVIDER=gemini. No request was made.")
        return 2
    key = settings.gemini_api_key.get_secret_value().strip() if settings.gemini_api_key is not None else ""
    if not key:
        print("NOT CONFIGURED: GEMINI_API_KEY is required (put it in the git-ignored backend/.env). No request was made.")
        return 2
    client = OpenAILLMClient.from_settings(settings)
    print(
        f"provider=gemini model={settings.llm_model or GEMINI_MODEL} "
        f"endpoint={settings.llm_base_url or GEMINI_BASE_URL} "
        f"reasoning_effort={settings.llm_reasoning_effort or GEMINI_REASONING_EFFORT or 'not sent'} "
        f"timeout={settings.llm_timeout_seconds}s max_retries=0"
    )
    checks = [
        ("reasoning decision", REASONING_SYSTEM_PROMPT, build_reasoning_user_prompt(REASONING_CONTEXT),
         REASONING_SCHEMA_NAME, reasoning_decision_json_schema(), parse_reasoning_decision),
        ("final output", FINAL_OUTPUT_SYSTEM_PROMPT, build_final_output_user_prompt(FINAL_CONTEXT),
         FINAL_OUTPUT_SCHEMA_NAME, final_output_json_schema(), parse_final_output),
    ]
    failed = False
    for label, system_prompt, user_prompt, schema_name, schema, parse in checks:
        try:
            raw = await client.generate_json(
                system_prompt=system_prompt, user_prompt=user_prompt, schema_name=schema_name, json_schema=schema
            )
            parsed = parse(raw)
        except InvalidLLMOutputError as err:
            print(f"[FAIL] {label}: the provider answered, but the output is invalid: {err}")
            failed = True
        except LLMError as err:
            print(f"[FAIL] {label}: {type(err).__name__}: {err}")
            failed = True
        else:
            print(f"[ OK ] {label}: valid -> {parsed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
