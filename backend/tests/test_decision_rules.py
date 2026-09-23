"""Workflow-side (deterministic) validation of decisions: the workflow does not trust the LLM."""

from app.temporal.constants import MEMORY_MAX_OPEN_CONCERNS, MEMORY_SUMMARY_MAX_CHARS
from app.temporal.contracts import ReasoningDecision
from app.temporal.decision_rules import (
    clamp_wake_interval,
    fallback_final_output,
    updated_memory,
    validate_decision,
)

ALL_TOOLS = ["get_order_status", "get_shipment_status", "escalate_shipment", "send_customer_update"]


def _decision(tool=None, tool_input=None, wake=None, summary="s", concerns=None) -> ReasoningDecision:
    return ReasoningDecision(
        assessment="a",
        tool=tool,
        tool_input=tool_input or {},
        next_wake_in_minutes=wake,
        situation_summary=summary,
        open_concerns=concerns or [],
    )


def _validate(decision, enabled=ALL_TOOLS):
    return validate_decision(decision, enabled_tools=enabled, default_wake=60, min_wake=5, max_wake=1440)


def test_accepts_enabled_tool_with_inputs():
    v = _validate(_decision("send_customer_update", {"message": "Your parcel is delayed"}))
    assert v.tool == "send_customer_update"
    assert v.tool_input == {"message": "Your parcel is delayed"}
    assert v.rejection_reason is None


def test_rejects_tool_not_enabled_for_supervisor():
    v = _validate(_decision("escalate_shipment", {"reason": "r", "priority": "high"}), enabled=["get_order_status"])
    assert v.tool is None
    assert "not enabled" in v.rejection_reason


def test_rejects_unknown_tool():
    v = _validate(_decision("refund_customer"))
    assert v.tool is None
    assert "unknown tool" in v.rejection_reason


def test_rejects_more_than_one_tool_independently_of_the_activity():
    v = _validate(_decision(["get_order_status", "get_shipment_status"]))
    assert v.tool is None
    assert "exactly one tool" in v.rejection_reason


def test_rejects_missing_required_input():
    v = _validate(_decision("escalate_shipment", {"reason": "late"}))
    assert v.tool is None
    assert "priority" in v.rejection_reason


def test_no_tool_is_not_a_rejection():
    v = _validate(_decision())
    assert v.tool is None and v.rejection_reason is None


def test_wake_interval_clamped_to_configured_bounds():
    assert clamp_wake_interval(None, 60, 5, 1440) == 60
    assert clamp_wake_interval(1, 60, 5, 1440) == 5
    assert clamp_wake_interval(-10, 60, 5, 1440) == 5
    assert clamp_wake_interval(10_000, 60, 5, 1440) == 1440
    assert clamp_wake_interval(30, 60, 5, 1440) == 30
    assert clamp_wake_interval(True, 60, 5, 1440) == 60  # bool is not an interval
    assert _validate(_decision(wake=2)).wake_minutes == 5


def test_memory_is_capped_and_workflow_owns_metadata():
    memory = updated_memory(
        _decision(summary="x" * 5000, concerns=[f"c{i}" * 300 for i in range(9)]),
        last_action="get_order_status: succeeded",
        wake_reason="important_event",
        cycle_count=3,
    )
    assert len(memory["situation_summary"]) == MEMORY_SUMMARY_MAX_CHARS
    assert len(memory["open_concerns"]) == MEMORY_MAX_OPEN_CONCERNS
    assert all(len(c) <= 200 for c in memory["open_concerns"])
    assert memory["last_action"] == "get_order_status: succeeded"
    assert memory["last_wake_reason"] == "important_event"
    assert memory["cycle_count"] == 3


def test_fallback_final_output_uses_only_recorded_state():
    content = fallback_final_output(
        order_id="42",
        final_order_status="delivered",
        memory={"situation_summary": "Delivered after a delay."},
        action_log=[{"tool": "escalate_shipment", "success": True}, {"tool": "send_customer_update", "success": False}],
        events_received=5,
        reasoning_count=3,
        failure="LLM unavailable",
    )
    assert "42" in content.summary and "delivered" in content.summary
    assert "2 tool execution(s), 1 failed" in content.summary
    assert content.key_actions == ["escalate_shipment: succeeded", "send_customer_update: failed"]
    assert "fallback" in content.key_learnings[0]
