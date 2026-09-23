"""Database-backed implementations of the four existing tools (Step 5).

The tools read and mutate the mock OPERATIONAL state (app/db/mock_models.py)
in PostgreSQL. They are handlers for the existing Step 4 registry: the tool
names, their side-effect classification and required inputs still come from
``TOOL_SPECS``; only the handler mapping is built here, from an injected
session factory (no connection is created at import time).

Error semantics (unchanged from Step 4):
- expected domain conditions (unknown order, no shipment, invalid input) ->
  ``ToolResult(success=False, error=...)``; never raised, so never retried;
- unexpected database/infrastructure errors propagate as exceptions, so the
  existing Activity retry policy applies (side-effecting tools: one attempt).

The order is always ``ToolRequest.order_id`` (from the workflow's run), never
a value chosen by the LLM.

Allowed side effects only: get_* never mutate; escalate_shipment may set
``mock_shipments.escalated``; send_customer_update inserts one outbound message.
"""

from typing import Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.mock_models import MockCustomerMessage, MockOrder, MockShipment
from app.temporal.contracts import ToolRequest, ToolResult
from app.tools.registry import ToolHandler, missing_required_inputs, validate_tool_handlers

ORDER_NOT_FOUND = "Order not found"
SHIPMENT_NOT_FOUND = "Shipment not found"


def _failure(error: str) -> ToolResult:
    return ToolResult(success=False, output={}, error=error)


async def _order(session: AsyncSession, order_id: str) -> Optional[MockOrder]:
    return (await session.execute(select(MockOrder).where(MockOrder.order_id == order_id))).scalar_one_or_none()


async def _shipment(session: AsyncSession, order_id: str, *, for_update: bool = False) -> Optional[MockShipment]:
    query = select(MockShipment).where(MockShipment.order_id == order_id)
    if for_update:
        query = query.with_for_update()
    return (await session.execute(query)).scalar_one_or_none()


def build_mock_tool_handlers(session_factory: async_sessionmaker) -> Dict[str, ToolHandler]:
    async def get_order_status(request: ToolRequest) -> ToolResult:
        async with session_factory() as session:
            order = await _order(session, request.order_id)
        if order is None:
            return _failure(ORDER_NOT_FOUND)
        return ToolResult(success=True, output={"order_id": order.order_id, "status": order.status})

    async def get_shipment_status(request: ToolRequest) -> ToolResult:
        async with session_factory() as session:
            if await _order(session, request.order_id) is None:
                return _failure(ORDER_NOT_FOUND)
            shipment = await _shipment(session, request.order_id)
        if shipment is None:
            return _failure(SHIPMENT_NOT_FOUND)
        return ToolResult(
            success=True,
            output={
                "order_id": shipment.order_id,
                "shipment_id": shipment.shipment_id,
                "status": shipment.status,
                "tracking_number": shipment.tracking_number,
                "delay_reason": shipment.delay_reason,
                "escalated": shipment.escalated,
            },
        )

    async def escalate_shipment(request: ToolRequest) -> ToolResult:
        missing = missing_required_inputs("escalate_shipment", request.tool_input)
        if missing:
            return _failure(f"Missing required inputs: {missing}")
        async with session_factory() as session:
            async with session.begin():
                if await _order(session, request.order_id) is None:
                    return _failure(ORDER_NOT_FOUND)
                shipment = await _shipment(session, request.order_id, for_update=True)
                if shipment is None:
                    return _failure(SHIPMENT_NOT_FOUND)
                if not shipment.escalated:  # idempotent: already escalated -> no write
                    shipment.escalated = True
                result = {"order_id": shipment.order_id, "shipment_id": shipment.shipment_id, "escalated": True}
        return ToolResult(success=True, output=result)

    async def send_customer_update(request: ToolRequest) -> ToolResult:
        message = request.tool_input.get("message")
        if not isinstance(message, str) or not message.strip():
            return _failure("Message must be a non-empty string")
        async with session_factory() as session:
            async with session.begin():
                if await _order(session, request.order_id) is None:
                    return _failure(ORDER_NOT_FOUND)
                row = MockCustomerMessage(order_id=request.order_id, direction="outbound", message=message.strip())
                session.add(row)
                await session.flush()
                message_id = str(row.id)
        return ToolResult(success=True, output={"order_id": request.order_id, "message_id": message_id})

    handlers: Dict[str, ToolHandler] = {
        "get_order_status": get_order_status,
        "get_shipment_status": get_shipment_status,
        "escalate_shipment": escalate_shipment,
        "send_customer_update": send_customer_update,
    }
    validate_tool_handlers(handlers)
    return handlers
