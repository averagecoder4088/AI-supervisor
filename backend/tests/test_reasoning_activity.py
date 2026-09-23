"""LLM Activities with FakeLLMClient: no network, no API key."""

from datetime import datetime, timezone

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from app.llm.client import (
    LLMAuthenticationError,
    LLMNotConfiguredError,
    LLMProviderError,
    OpenAILLMClient,
)
from app.llm.fake import FakeLLMClient, make_decision_json, make_final_output_json
from app.llm.schemas import FINAL_OUTPUT_SCHEMA_NAME, REASONING_SCHEMA_NAME
from app.temporal.activities.reasoning import ReasoningActivities
from app.temporal.contracts import FinalOutputInput, ReasoningInput
from app.temporal.types import OrderEvent

pytestmark = pytest.mark.asyncio


def _context(**overrides) -> ReasoningInput:
    values = dict(
        order_id="12345",
        order_status="delayed",
        wake_reason="important_event",
        supervisor_instructions="Keep customers informed.",
        supervisor_version=1,
        run_instructions=["If shipment is delayed, escalate immediately."],
        enabled_tools=["get_shipment_status", "escalate_shipment"],
        memory={"situation_summary": "Shipped yesterday.", "open_concerns": []},
        new_events=[OrderEvent(event_type="shipment_delayed", payload={"hours": 8})],
        recent_events=[OrderEvent(event_type="shipment_created")],
        last_action_result=None,
        default_wake_interval_minutes=60,
        min_wake_interval_minutes=5,
        max_wake_interval_minutes=1440,
        now=datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc),
    )
    values.update(overrides)
    return ReasoningInput(**values)


def _final_context() -> FinalOutputInput:
    return FinalOutputInput(
        order_id="12345",
        final_order_status="delivered",
        supervisor_instructions="",
        run_instructions=[],
        memory={},
        recent_events=[],
        action_log=[],
        events_received=3,
        reasoning_count=2,
    )


async def _decide(llm: FakeLLMClient, context=None):
    return await ActivityEnvironment().run(ReasoningActivities(llm).generate_reasoning_decision, context or _context())


async def test_fake_llm_client_scripted_default_and_call_recording():
    llm = FakeLLMClient(["scripted"])
    args = dict(system_prompt="s", user_prompt="u", json_schema={})
    assert await llm.generate_json(schema_name=REASONING_SCHEMA_NAME, **args) == "scripted"
    assert "No action needed" in await llm.generate_json(schema_name=REASONING_SCHEMA_NAME, **args)
    assert "summary" in await llm.generate_json(schema_name=FINAL_OUTPUT_SCHEMA_NAME, **args)
    assert len(llm.calls) == 3


async def test_reasoning_activity_returns_validated_decision():
    llm = FakeLLMClient([make_decision_json(tool="escalate_shipment", reason="8h delay", priority="high")])
    decision = await _decide(llm)
    assert decision.tool == "escalate_shipment"
    assert decision.tool_input == {"reason": "8h delay", "priority": "high"}

    call = llm.calls[0]
    assert call["schema_name"] == REASONING_SCHEMA_NAME
    assert call["json_schema"]["additionalProperties"] is False
    # Compact context: instructions, new events and ONLY the enabled tools.
    assert "If shipment is delayed, escalate immediately." in call["user_prompt"]
    assert "shipment_delayed" in call["user_prompt"]
    assert "escalate_shipment" in call["user_prompt"]
    assert "send_customer_update" not in call["user_prompt"]


async def test_invalid_llm_output_is_a_retryable_application_error():
    with pytest.raises(ApplicationError) as exc:
        await _decide(FakeLLMClient(["{not json"]))
    assert exc.value.type == "InvalidLLMOutput"
    assert exc.value.non_retryable is False


async def test_authentication_error_is_non_retryable():
    with pytest.raises(ApplicationError) as exc:
        await _decide(FakeLLMClient([LLMAuthenticationError("bad key")]))
    assert exc.value.type == "LLMAuthenticationError"
    assert exc.value.non_retryable is True


async def test_provider_error_is_retryable():
    with pytest.raises(ApplicationError) as exc:
        await _decide(FakeLLMClient([LLMProviderError("rate limited")]))
    assert exc.value.type == "LLMProviderError"
    assert exc.value.non_retryable is False


async def test_deferred_openai_client_fails_non_retryably_without_network():
    with pytest.raises(LLMNotConfiguredError):
        await OpenAILLMClient().generate_json(system_prompt="", user_prompt="", schema_name="x", json_schema={})
    with pytest.raises(ApplicationError) as exc:
        await ActivityEnvironment().run(
            ReasoningActivities(OpenAILLMClient()).generate_reasoning_decision, _context()
        )
    assert exc.value.type == "LLMNotConfigured"
    assert exc.value.non_retryable is True


async def test_final_output_activity_success_and_invalid_output():
    activities = ReasoningActivities(FakeLLMClient([make_final_output_json(summary="Delivered."), "[]"]))
    content = await ActivityEnvironment().run(activities.generate_final_output, _final_context())
    assert content.summary == "Delivered."
    with pytest.raises(ApplicationError) as exc:
        await ActivityEnvironment().run(activities.generate_final_output, _final_context())
    assert exc.value.type == "InvalidLLMOutput"
