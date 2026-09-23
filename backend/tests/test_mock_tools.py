"""The four tools against the PostgreSQL mock operational state (rolled back)."""

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.db.mock_factories import create_mock_order, create_mock_shipment
from app.db.mock_models import MockCustomerMessage, MockOrder, MockShipment
from app.temporal.contracts import ToolRequest
from app.tools.mock_operations import build_mock_tool_handlers

pytestmark = pytest.mark.asyncio

PAST = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _request(tool: str, order_id: str = "ORD-001", **tool_input) -> ToolRequest:
    return ToolRequest(tool_execution_id=str(uuid.uuid4()), order_id=order_id, tool_name=tool, tool_input=tool_input)


@pytest_asyncio.fixture
async def tools(db_session_factory):
    return build_mock_tool_handlers(db_session_factory)


@pytest_asyncio.fixture
async def order_with_shipment(db_session_factory):
    async with db_session_factory() as session:
        await create_mock_order(session, order_id="ORD-001", status="payment_confirmed", created_at=PAST)
        await create_mock_shipment(
            session,
            order_id="ORD-001",
            shipment_id="SHIP-001",
            status="delayed",
            tracking_number="TRK-001",
            delay_reason="Carrier delay",
        )
        await session.commit()


@pytest_asyncio.fixture
async def order_without_shipment(db_session_factory):
    async with db_session_factory() as session:
        await create_mock_order(session, order_id="ORD-002", status="created")
        await session.commit()


async def _order(factory, order_id="ORD-001") -> MockOrder:
    async with factory() as session:
        return (await session.execute(select(MockOrder).where(MockOrder.order_id == order_id))).scalar_one()


async def _shipment(factory, order_id="ORD-001") -> MockShipment:
    async with factory() as session:
        return (await session.execute(select(MockShipment).where(MockShipment.order_id == order_id))).scalar_one()


async def _messages(factory):
    async with factory() as session:
        return (await session.execute(select(MockCustomerMessage))).scalars().all()


def _snapshot(row) -> dict:
    return {c.name: getattr(row, c.key) for c in row.__table__.columns}


# ------------------------------------------------------------ get_order_status


async def test_get_order_status_returns_current_status(tools, order_with_shipment):
    result = await tools["get_order_status"](_request("get_order_status"))
    assert result.success is True
    assert result.output == {"order_id": "ORD-001", "status": "payment_confirmed"}
    assert result.error is None


async def test_get_order_status_unknown_order_is_business_failure(tools):
    result = await tools["get_order_status"](_request("get_order_status", order_id="ORD-404"))
    assert (result.success, result.error, result.output) == (False, "Order not found", {})


async def test_get_order_status_does_not_mutate(tools, order_with_shipment, db_session_factory):
    before = _snapshot(await _order(db_session_factory))
    await tools["get_order_status"](_request("get_order_status"))
    assert _snapshot(await _order(db_session_factory)) == before


# --------------------------------------------------------- get_shipment_status


async def test_get_shipment_status_returns_complete_result(tools, order_with_shipment):
    result = await tools["get_shipment_status"](_request("get_shipment_status"))
    assert result.success is True
    assert result.output == {
        "order_id": "ORD-001",
        "shipment_id": "SHIP-001",
        "status": "delayed",
        "tracking_number": "TRK-001",
        "delay_reason": "Carrier delay",
        "escalated": False,
    }


async def test_get_shipment_status_unknown_order_is_business_failure(tools):
    result = await tools["get_shipment_status"](_request("get_shipment_status", order_id="ORD-404"))
    assert (result.success, result.error) == (False, "Order not found")


async def test_get_shipment_status_order_without_shipment_is_business_failure(tools, order_without_shipment):
    result = await tools["get_shipment_status"](_request("get_shipment_status", order_id="ORD-002"))
    assert (result.success, result.error) == (False, "Shipment not found")


async def test_get_shipment_status_does_not_mutate(tools, order_with_shipment, db_session_factory):
    before = _snapshot(await _shipment(db_session_factory))
    await tools["get_shipment_status"](_request("get_shipment_status"))
    assert _snapshot(await _shipment(db_session_factory)) == before


# ----------------------------------------------------------- escalate_shipment


async def test_delayed_shipment_is_escalated(tools, order_with_shipment, db_session_factory):
    before = await _shipment(db_session_factory)
    assert before.escalated is False

    result = await tools["escalate_shipment"](
        _request("escalate_shipment", reason="Shipment has been delayed for 24 hours", priority="high")
    )
    assert result.success is True
    assert result.output == {"order_id": "ORD-001", "shipment_id": "SHIP-001", "escalated": True}

    after = await _shipment(db_session_factory)
    assert after.escalated is True  # false -> true
    assert (after.status, after.delay_reason, after.tracking_number) == ("delayed", "Carrier delay", "TRK-001")
    assert await _messages(db_session_factory) == []  # no hidden side effects
    assert _snapshot(await _order(db_session_factory))["status"] == "payment_confirmed"


async def test_repeated_escalation_is_idempotent(tools, order_with_shipment, db_session_factory):
    request = _request("escalate_shipment", reason="late", priority="high")
    await tools["escalate_shipment"](request)
    once = _snapshot(await _shipment(db_session_factory))

    again = await tools["escalate_shipment"](_request("escalate_shipment", reason="still late", priority="high"))
    assert again.success is True
    assert again.output["escalated"] is True
    assert _snapshot(await _shipment(db_session_factory)) == once  # no second write


async def test_escalate_unknown_order_is_business_failure(tools):
    result = await tools["escalate_shipment"](
        _request("escalate_shipment", order_id="ORD-404", reason="late", priority="high")
    )
    assert (result.success, result.error) == (False, "Order not found")


async def test_escalate_missing_shipment_is_business_failure(tools, order_without_shipment):
    result = await tools["escalate_shipment"](
        _request("escalate_shipment", order_id="ORD-002", reason="late", priority="high")
    )
    assert (result.success, result.error) == (False, "Shipment not found")


async def test_escalate_requires_existing_tool_input_conventions(tools, order_with_shipment, db_session_factory):
    result = await tools["escalate_shipment"](_request("escalate_shipment", reason="   ", priority="high"))
    assert result.success is False and "reason" in result.error
    assert (await _shipment(db_session_factory)).escalated is False


# -------------------------------------------------------- send_customer_update


async def test_valid_update_creates_one_outbound_message(tools, order_with_shipment, db_session_factory):
    text = "Your shipment has been escalated and we are following up with the carrier."
    result = await tools["send_customer_update"](_request("send_customer_update", message=text))
    assert result.success is True
    assert result.output["order_id"] == "ORD-001"

    (row,) = await _messages(db_session_factory)
    assert str(row.id) == result.output["message_id"]  # returned id is the inserted row
    assert (row.order_id, row.direction, row.message) == ("ORD-001", "outbound", text)
    assert row.created_at.tzinfo is not None
    assert (await _shipment(db_session_factory)).escalated is False  # no hidden side effects


async def test_send_update_unknown_order_is_business_failure(tools, db_session_factory):
    result = await tools["send_customer_update"](_request("send_customer_update", order_id="ORD-404", message="hi"))
    assert (result.success, result.error) == (False, "Order not found")
    assert await _messages(db_session_factory) == []


@pytest.mark.parametrize("message", ["", "   ", None, 42])
async def test_send_update_invalid_message_is_business_failure(tools, order_with_shipment, db_session_factory, message):
    result = await tools["send_customer_update"](_request("send_customer_update", message=message))
    assert result.success is False
    assert result.error == "Message must be a non-empty string"
    assert await _messages(db_session_factory) == []
