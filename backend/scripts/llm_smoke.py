"""Manual live check of the OpenAI adapter (decision B7). NOT part of the pytest suite.

Sends one real reasoning request and one real final-output request with the
application's own prompts and strict schemas, and reports whether the model's
answers pass the same local validation the workflow uses. It is the only way to
confirm that the configured model ID exists and that the provider accepts the
generated JSON schemas.

Needs LLM_API_KEY and LLM_MODEL (git-ignored backend/.env or the environment):

    PYTHONPATH=backend .venv/bin/python backend/scripts/llm_smoke.py

Exit code: 0 both checks passed, 1 a check failed, 2 not configured.
"""

import asyncio
import sys
from datetime import datetime, timezone

from app.config import get_settings
from app.llm.client import LLMError, OpenAILLMClient
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
    client = OpenAILLMClient.from_settings(get_settings())
    print(f"client: {client!r}")
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
            if type(err).__name__ == "LLMNotConfiguredError" and "not configured" in str(err):
                return 2
            failed = True
        else:
            print(f"[ OK ] {label}: valid -> {parsed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
