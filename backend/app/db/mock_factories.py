"""Deterministic factories for mock operational state (Step 5).

Explicit scenario construction for tests and the later simulation phase:
every value is either passed in or derived deterministically from the ids.
Nothing is random, and nothing is inserted unless a caller asks for it.

Factories add and flush within the caller's session; the caller commits.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.mock_models import (
    MESSAGE_DIRECTIONS,
    ORDER_STATUSES,
    SHIPMENT_STATUSES,
    MockCustomerMessage,
    MockOrder,
    MockShipment,
)


def _check(value: str, allowed: frozenset, name: str) -> None:
    if value not in allowed:
        raise ValueError(f"invalid {name} {value!r}; allowed: {sorted(allowed)}")


async def create_mock_order(
    session: AsyncSession,
    *,
    order_id: str,
    status: str = "created",
    customer_id: Optional[str] = None,
    created_at: Optional[datetime] = None,
) -> MockOrder:
    _check(status, ORDER_STATUSES, "order status")
    order = MockOrder(order_id=order_id, status=status, customer_id=customer_id or f"CUST-{order_id}")
    if created_at is not None:
        order.created_at = created_at
        order.updated_at = created_at
    session.add(order)
    await session.flush()
    return order


async def create_mock_shipment(
    session: AsyncSession,
    *,
    order_id: str,
    shipment_id: str,
    status: str = "created",
    tracking_number: Optional[str] = None,
    delay_reason: Optional[str] = None,
    escalated: bool = False,
) -> MockShipment:
    _check(status, SHIPMENT_STATUSES, "shipment status")
    shipment = MockShipment(
        order_id=order_id,
        shipment_id=shipment_id,
        status=status,
        tracking_number=tracking_number or f"TRK-{shipment_id}",
        delay_reason=delay_reason,
        escalated=escalated,
    )
    session.add(shipment)
    await session.flush()
    return shipment


async def create_mock_customer_message(
    session: AsyncSession,
    *,
    order_id: str,
    message: str,
    direction: str = "inbound",
) -> MockCustomerMessage:
    _check(direction, MESSAGE_DIRECTIONS, "message direction")
    if not message.strip():
        raise ValueError("message must be non-empty")
    row = MockCustomerMessage(order_id=order_id, direction=direction, message=message)
    session.add(row)
    await session.flush()
    return row
