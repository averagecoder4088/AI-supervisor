"""Tool registry and the execute_tool Activity."""

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from app.temporal.activities.tools import ToolActivities
from app.temporal.contracts import ToolRequest, ToolResult
from app.tools.registry import TOOL_SPECS, missing_required_inputs, validate_tool_handlers


def _request(tool: str, **tool_input) -> ToolRequest:
    return ToolRequest(tool_execution_id="te-1", order_id="12345", tool_name=tool, tool_input=tool_input)


async def _run(activities: ToolActivities, request: ToolRequest) -> ToolResult:
    return await ActivityEnvironment().run(activities.execute_tool, request)


def test_registry_contains_exactly_the_four_fixed_tools():
    assert set(TOOL_SPECS) == {"get_order_status", "get_shipment_status", "escalate_shipment", "send_customer_update"}
    assert {n for n, s in TOOL_SPECS.items() if s.side_effecting} == {"escalate_shipment", "send_customer_update"}
    assert missing_required_inputs("escalate_shipment", {"reason": "x"}) == ["priority"]
    assert missing_required_inputs("get_order_status", {}) == []


def test_registry_rejects_handlers_for_invented_tools():
    async def handler(request):
        return ToolResult(success=True)

    with pytest.raises(ValueError):
        validate_tool_handlers({"refund_customer": handler})
    with pytest.raises(ValueError):
        ToolActivities({"refund_customer": handler})


@pytest.mark.asyncio
async def test_default_stubs_succeed_deterministically():
    activities = ToolActivities()
    first = await _run(activities, _request("send_customer_update", message="hi"))
    again = await _run(activities, _request("send_customer_update", message="hi"))
    assert first.success is True
    assert first.output == again.output == {"message_id": "msg-te-1", "source": "step4_stub"}
    escalation = await _run(activities, _request("escalate_shipment", reason="late", priority="high"))
    assert escalation.output["escalation_id"] == "esc-te-1"
    assert (await _run(activities, _request("get_order_status"))).success is True
    assert (await _run(activities, _request("get_shipment_status"))).success is True


@pytest.mark.asyncio
async def test_business_failure_is_returned_not_raised():
    async def not_found(request):
        return ToolResult(success=False, output={}, error="shipment not found")

    result = await _run(ToolActivities({"get_shipment_status": not_found}), _request("get_shipment_status"))
    assert result.success is False
    assert result.error == "shipment not found"


@pytest.mark.asyncio
async def test_unexpected_crash_propagates_for_temporal_retry():
    async def crash(request):
        raise RuntimeError("carrier API exploded")

    with pytest.raises(RuntimeError, match="exploded"):
        await _run(ToolActivities({"get_shipment_status": crash}), _request("get_shipment_status"))


@pytest.mark.asyncio
async def test_unregistered_tool_is_a_non_retryable_error():
    with pytest.raises(ApplicationError) as exc:
        await _run(ToolActivities({}), _request("get_order_status"))
    assert exc.value.type == "UnknownTool"
    assert exc.value.non_retryable is True
