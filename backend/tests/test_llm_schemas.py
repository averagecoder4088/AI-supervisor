"""Structured LLM output: strict validation of the reasoning decision and final output."""

import json
import typing

import pytest

from app.llm.fake import make_decision_json, make_final_output_json
from app.llm.schemas import (
    InvalidLLMOutputError,
    ToolName,
    final_output_json_schema,
    parse_final_output,
    parse_reasoning_decision,
    reasoning_decision_json_schema,
)
from app.tools.registry import TOOL_NAMES


def _decision(**changes) -> str:
    data = json.loads(make_decision_json())
    data.update(changes)
    return json.dumps(data)


def test_valid_no_tool_decision_parses():
    decision = parse_reasoning_decision(make_decision_json(next_wake_in_minutes=30, open_concerns=["late"]))
    assert decision.tool is None
    assert decision.tool_input == {}
    assert decision.next_wake_in_minutes == 30
    assert decision.open_concerns == ["late"]


def test_valid_tool_decision_keeps_only_provided_inputs():
    decision = parse_reasoning_decision(
        make_decision_json(tool="escalate_shipment", reason="late 2 days", priority="high")
    )
    assert decision.tool == "escalate_shipment"
    assert decision.tool_input == {"reason": "late 2 days", "priority": "high"}


def test_invalid_tool_name_is_rejected():
    with pytest.raises(InvalidLLMOutputError):
        parse_reasoning_decision(make_decision_json(tool="refund_customer"))


def test_multiple_tools_are_rejected():
    with pytest.raises(InvalidLLMOutputError):
        parse_reasoning_decision(_decision(tool=["get_order_status", "get_shipment_status"]))


@pytest.mark.parametrize(
    "raw",
    [
        make_decision_json(tool="send_customer_update"),  # no message
        make_decision_json(tool="send_customer_update", message="   "),  # blank message
        make_decision_json(tool="escalate_shipment", reason="late"),  # no priority
        make_decision_json(tool="escalate_shipment", priority="high"),  # no reason
    ],
)
def test_missing_required_tool_input_is_rejected(raw):
    with pytest.raises(InvalidLLMOutputError):
        parse_reasoning_decision(raw)


def test_invalid_priority_value_is_rejected():
    with pytest.raises(InvalidLLMOutputError):
        parse_reasoning_decision(make_decision_json(tool="escalate_shipment", reason="late", priority="urgent"))


@pytest.mark.parametrize(
    "raw",
    [
        _decision(extra_field=True),  # arbitrary extra field
        _decision(next_wake_in_minutes="30"),  # no string -> int coercion
        _decision(next_wake_in_minutes=12.5),
        _decision(assessment=""),
        _decision(memory_update={"situation_summary": "x"}),  # missing open_concerns
        _decision(memory_update={"situation_summary": "x", "open_concerns": "late"}),
        "not json at all",
        "",
    ],
)
def test_malformed_decisions_are_rejected(raw):
    with pytest.raises(InvalidLLMOutputError):
        parse_reasoning_decision(raw)


def test_tool_literal_matches_the_fixed_registry():
    assert set(typing.get_args(ToolName)) == set(TOOL_NAMES)


def test_json_schemas_are_strict():
    schema = reasoning_decision_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"assessment", "tool", "tool_input", "next_wake_in_minutes", "memory_update"}
    final = final_output_json_schema()
    assert final["additionalProperties"] is False
    assert set(final["required"]) == {"summary", "key_actions", "key_learnings", "recommendations"}


def test_final_output_parses_and_rejects_missing_fields():
    content = parse_final_output(make_final_output_json(summary="done", key_actions=["escalated"]))
    assert content.summary == "done"
    assert content.key_actions == ["escalated"]
    with pytest.raises(InvalidLLMOutputError):
        parse_final_output(json.dumps({"summary": "done"}))
