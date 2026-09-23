"""The fixed mock tool registry (Step 4 boundary; Step 5 makes the mocks realistic).

Only these four tools exist. Each has metadata the workflow uses to validate a
decision and choose a retry policy (``TOOL_SPECS``, pure data, safe to import
in workflow code), and a handler the ``execute_tool`` Activity dispatches to.

The Step 4 handlers are minimal deterministic stubs. They perform no external
side effects; they only make the Activity boundary testable end to end.
"""

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Tuple

from app.temporal.contracts import ToolRequest, ToolResult


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    # Side-effecting tools are never retried automatically (duplicate risk).
    side_effecting: bool
    required_inputs: Tuple[str, ...] = ()


TOOL_SPECS: Dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec(
            name="get_order_status",
            description="Read the current status of the order.",
            side_effecting=False,
        ),
        ToolSpec(
            name="get_shipment_status",
            description="Read the current shipment/tracking status of the order.",
            side_effecting=False,
        ),
        ToolSpec(
            name="escalate_shipment",
            description="Escalate a shipment problem to the operations team. Requires reason and priority.",
            side_effecting=True,
            required_inputs=("reason", "priority"),
        ),
        ToolSpec(
            name="send_customer_update",
            description="Send a message to the customer. Requires message.",
            side_effecting=True,
            required_inputs=("message",),
        ),
    )
}

TOOL_NAMES: Tuple[str, ...] = tuple(TOOL_SPECS)

ToolHandler = Callable[[ToolRequest], Awaitable[ToolResult]]


def missing_required_inputs(tool_name: str, tool_input: Mapping[str, Any]) -> List[str]:
    """Names of required inputs that are absent or blank for ``tool_name``."""
    spec = TOOL_SPECS[tool_name]
    return [
        name
        for name in spec.required_inputs
        if not isinstance(tool_input.get(name), str) or not tool_input[name].strip()
    ]


# ------------------------------------------------- Step 4 deterministic stubs


async def _get_order_status(request: ToolRequest) -> ToolResult:
    return ToolResult(
        success=True,
        output={"order_id": request.order_id, "order_status": "unknown", "source": "step4_stub"},
    )


async def _get_shipment_status(request: ToolRequest) -> ToolResult:
    return ToolResult(
        success=True,
        output={"order_id": request.order_id, "shipment_status": "unknown", "source": "step4_stub"},
    )


async def _escalate_shipment(request: ToolRequest) -> ToolResult:
    return ToolResult(
        success=True,
        output={
            # Derived from the idempotency key: the same execution always
            # yields the same escalation id.
            "escalation_id": f"esc-{request.tool_execution_id}",
            "priority": request.tool_input.get("priority"),
            "source": "step4_stub",
        },
    )


async def _send_customer_update(request: ToolRequest) -> ToolResult:
    return ToolResult(
        success=True,
        output={"message_id": f"msg-{request.tool_execution_id}", "source": "step4_stub"},
    )


DEFAULT_TOOL_HANDLERS: Dict[str, ToolHandler] = {
    "get_order_status": _get_order_status,
    "get_shipment_status": _get_shipment_status,
    "escalate_shipment": _escalate_shipment,
    "send_customer_update": _send_customer_update,
}


def validate_tool_handlers(handlers: Mapping[str, ToolHandler]) -> None:
    """Reject any handler for a tool outside the fixed vocabulary."""
    unknown = sorted(set(handlers) - set(TOOL_SPECS))
    if unknown:
        raise ValueError(f"Unknown tools {unknown}; only {list(TOOL_NAMES)} exist")
