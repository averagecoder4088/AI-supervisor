"""The external world: mock operational changes first, then the event through the API.

Only ``mock_orders``, ``mock_shipments`` and ``mock_customer_messages`` are
written here, through the Step 5 factories. Every value is deterministic.

Each step commits its operational change BEFORE the event is submitted, so a
supervisor woken by that event (or a later one) reads the state the event
describes. A non-202 response from the event endpoint raises: there is no retry
and no reconciliation, so a lost event can never pass silently.
"""

from typing import Any, Dict, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.mock_factories import create_mock_customer_message, create_mock_order, create_mock_shipment
from app.db.mock_models import MockOrder, MockShipment

# Order status implied by each simulated event (approved S1 transitions). The
# supervisor's ``order_status_by_event`` must agree with this (checked by tests).
EVENT_ORDER_STATUS: Dict[str, str] = {
    "order_created": "created",
    "payment_confirmed": "payment_confirmed",
    "shipment_created": "shipped",
    "delivered": "delivered",
}

# Sealed S1 keeps its own table above. The delayed-shipment scenario (S2) adds one event.
SHIPMENT_DELAYED_ORDER_STATUS = "delayed"
EVENT_ORDER_STATUS_WITH_DELAY: Dict[str, str] = {
    **EVENT_ORDER_STATUS,
    "shipment_delayed": SHIPMENT_DELAYED_ORDER_STATUS,
}

# The payment-failure / cancellation scenario (S5) adds two more events.
PAYMENT_FAILED_ORDER_STATUS = "payment_failed"
CANCELLED_ORDER_STATUS = "cancelled"
EVENT_ORDER_STATUS_WITH_CANCELLATION: Dict[str, str] = {
    **EVENT_ORDER_STATUS_WITH_DELAY,
    "payment_failed": PAYMENT_FAILED_ORDER_STATUS,
    "order_cancelled": CANCELLED_ORDER_STATUS,
}

# Cancellation is valid only before a shipment exists, from these order states.
CANCELLABLE_ORDER_STATUSES = ("created", "payment_confirmed", "payment_failed")

# Delivery is valid only from these states (a delayed shipment can still be delivered).
DELIVERABLE_ORDER_STATUSES = ("shipped", "delayed")
DELIVERABLE_SHIPMENT_STATUSES = ("created", "in_transit", "delayed")

PAYMENT_PAYLOAD: Dict[str, Any] = {"amount": 49.99, "currency": "USD"}


class ScenarioError(RuntimeError):
    """Base class for simulator failures."""


class EventNotAccepted(ScenarioError):
    """The event endpoint did not answer 202."""


class InvalidTransition(ScenarioError):
    """The requested world change is impossible from the current operational state."""


def shipment_id_for(order_id: str) -> str:
    return f"SHIP-{order_id}"


class ExternalWorld:
    def __init__(self, session_factory: async_sessionmaker, api: httpx.AsyncClient) -> None:
        self._session_factory = session_factory
        self._api = api

    # ------------------------------------------------------------ world steps

    async def place_order(self, order_id: str) -> None:
        """The customer places the order: the minimum mock order row. No event is sent."""
        async with self._session_factory() as session:
            async with session.begin():
                await create_mock_order(session, order_id=order_id, status=EVENT_ORDER_STATUS["order_created"])

    async def order_created(self, run_id: str, order_id: str) -> None:
        """Notify the supervisor that the (already existing) order was created. No DB change."""
        async with self._session_factory() as session:
            order = await self._order(session, order_id)
        await self._emit(run_id, "order_created", {"customer_id": order.customer_id})

    async def confirm_payment(self, run_id: str, order_id: str) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                order = await self._order(session, order_id, for_update=True)
                self._require(order.status == "created", order, "confirm payment")
                order.status = EVENT_ORDER_STATUS["payment_confirmed"]
        await self._emit(run_id, "payment_confirmed", dict(PAYMENT_PAYLOAD))

    async def create_shipment(self, run_id: str, order_id: str) -> None:
        """One transaction: the shipment row and the order status change."""
        async with self._session_factory() as session:
            async with session.begin():
                order = await self._order(session, order_id, for_update=True)
                self._require(order.status == "payment_confirmed", order, "create a shipment")
                shipment = await create_mock_shipment(
                    session, order_id=order_id, shipment_id=shipment_id_for(order_id), status="created"
                )
                order.status = EVENT_ORDER_STATUS["shipment_created"]
                payload = {"shipment_id": shipment.shipment_id, "tracking_number": shipment.tracking_number}
        await self._emit(run_id, "shipment_created", payload)

    async def delay_shipment(self, run_id: str, order_id: str, delay_reason: str) -> None:
        """One transaction: the shipment is delayed and the order follows. ``escalated`` is untouched."""
        async with self._session_factory() as session:
            async with session.begin():
                order = await self._order(session, order_id, for_update=True)
                self._require(order.status == "shipped", order, "delay the shipment")
                shipment = await self._shipment(session, order_id)
                if shipment.status not in ("created", "in_transit"):
                    raise InvalidTransition(f"cannot delay: shipment of {order_id!r} is {shipment.status!r}")
                shipment.status = "delayed"
                shipment.delay_reason = delay_reason
                order.status = SHIPMENT_DELAYED_ORDER_STATUS
                payload = {"shipment_id": shipment.shipment_id, "delay_reason": delay_reason}
        await self._emit(run_id, "shipment_delayed", payload)

    async def customer_message(self, run_id: str, order_id: str, message: str) -> None:
        """The customer writes in: one INBOUND message row. The order and shipment are untouched."""
        async with self._session_factory() as session:
            async with session.begin():
                await self._order(session, order_id)
                await create_mock_customer_message(session, order_id=order_id, message=message, direction="inbound")
        await self._emit(run_id, "customer_message_received", {"message": message})

    async def deliver(self, run_id: str, order_id: str) -> None:
        """One transaction: shipment and order become delivered. Other shipment fields are kept."""
        async with self._session_factory() as session:
            async with session.begin():
                order = await self._order(session, order_id, for_update=True)
                self._require(order.status in DELIVERABLE_ORDER_STATUSES, order, "deliver")
                shipment = await self._shipment(session, order_id)
                if shipment.status not in DELIVERABLE_SHIPMENT_STATUSES:
                    raise InvalidTransition(f"cannot deliver: shipment of {order_id!r} is {shipment.status!r}")
                shipment.status = "delivered"
                order.status = EVENT_ORDER_STATUS["delivered"]
                payload = {"shipment_id": shipment.shipment_id}
        await self._emit(run_id, "delivered", payload)

    async def fail_payment(self, run_id: str, order_id: str, reason: str) -> None:
        """The payment fails: the order goes created -> payment_failed. The reason travels in the event only."""
        async with self._session_factory() as session:
            async with session.begin():
                order = await self._order(session, order_id, for_update=True)
                self._require(order.status == "created", order, "fail the payment")
                order.status = PAYMENT_FAILED_ORDER_STATUS
        await self._emit(run_id, "payment_failed", {"reason": reason})

    async def cancel_order(self, run_id: str, order_id: str, reason: str) -> None:
        """The order is cancelled. Only before a shipment exists (shipment cancellation is not simulated)."""
        async with self._session_factory() as session:
            async with session.begin():
                order = await self._order(session, order_id, for_update=True)
                shipment = (
                    await session.execute(select(MockShipment).where(MockShipment.order_id == order_id))
                ).scalar_one_or_none()
                if shipment is not None:
                    raise InvalidTransition(f"cannot cancel: order {order_id!r} already has a shipment")
                self._require(order.status in CANCELLABLE_ORDER_STATUSES, order, "cancel")
                order.status = CANCELLED_ORDER_STATUS
        await self._emit(run_id, "order_cancelled", {"reason": reason})

    # ---------------------------------------------------------------- helpers

    @staticmethod
    async def _order(session: AsyncSession, order_id: str, *, for_update: bool = False) -> MockOrder:
        query = select(MockOrder).where(MockOrder.order_id == order_id)
        if for_update:
            query = query.with_for_update()
        order: Optional[MockOrder] = (await session.execute(query)).scalar_one_or_none()
        if order is None:
            raise InvalidTransition(f"mock order {order_id!r} does not exist")
        return order

    @staticmethod
    async def _shipment(session: AsyncSession, order_id: str) -> MockShipment:
        shipment: Optional[MockShipment] = (
            await session.execute(select(MockShipment).where(MockShipment.order_id == order_id).with_for_update())
        ).scalar_one_or_none()
        if shipment is None:
            raise InvalidTransition(f"order {order_id!r} has no shipment")
        return shipment

    @staticmethod
    def _require(condition: bool, order: MockOrder, action: str) -> None:
        if not condition:
            raise InvalidTransition(f"cannot {action}: order {order.order_id!r} is {order.status!r}")

    async def _emit(self, run_id: str, event_type: str, payload: Dict[str, Any]) -> None:
        response = await self._api.post(
            f"/api/runs/{run_id}/events", json={"event_type": event_type, "payload": payload}
        )
        if response.status_code != 202:
            raise EventNotAccepted(
                f"{event_type} was not accepted for run {run_id}: HTTP {response.status_code} {response.text}"
            )
