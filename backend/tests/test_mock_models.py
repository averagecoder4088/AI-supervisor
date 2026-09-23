"""Mock operational tables on the REAL local PostgreSQL (rolled back after each test)."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.base import Base
from app.db.mock_factories import create_mock_customer_message, create_mock_order, create_mock_shipment
from app.db.mock_models import MockBase, MockCustomerMessage, MockOrder

pytestmark = pytest.mark.asyncio


async def _expect_integrity_error(session, coro) -> None:
    with pytest.raises(IntegrityError):
        await coro
    await session.rollback()


async def test_mock_tables_are_separate_from_supervisor_metadata():
    assert set(MockBase.metadata.tables) == {"mock_orders", "mock_shipments", "mock_customer_messages"}
    assert not set(MockBase.metadata.tables) & set(Base.metadata.tables)


async def test_mock_order_can_be_inserted(db_session_factory):
    async with db_session_factory() as session:
        await create_mock_order(session, order_id="ORD-001", status="payment_confirmed", customer_id="CUST-9")
        await session.commit()
    async with db_session_factory() as session:
        order = (await session.execute(select(MockOrder).where(MockOrder.order_id == "ORD-001"))).scalar_one()
    assert (order.status, order.customer_id) == ("payment_confirmed", "CUST-9")
    assert order.created_at.tzinfo is not None and order.updated_at.tzinfo is not None


async def test_order_id_is_unique(db_session_factory):
    async with db_session_factory() as session:
        await create_mock_order(session, order_id="ORD-001")
        await session.commit()
        await _expect_integrity_error(session, create_mock_order(session, order_id="ORD-001"))


async def test_shipment_references_a_valid_order(db_session_factory):
    async with db_session_factory() as session:
        await create_mock_order(session, order_id="ORD-001")
        shipment = await create_mock_shipment(session, order_id="ORD-001", shipment_id="SHIP-001", status="in_transit")
        await session.commit()
    assert shipment.escalated is False
    assert shipment.tracking_number == "TRK-SHIP-001"  # deterministic default
    assert shipment.delay_reason is None


async def test_shipment_for_unknown_order_is_rejected(db_session_factory):
    async with db_session_factory() as session:
        await _expect_integrity_error(session, create_mock_shipment(session, order_id="ORD-404", shipment_id="SHIP-404"))


async def test_shipment_id_is_unique_and_one_shipment_per_order(db_session_factory):
    async with db_session_factory() as session:
        await create_mock_order(session, order_id="ORD-001")
        await create_mock_order(session, order_id="ORD-002")
        await create_mock_shipment(session, order_id="ORD-001", shipment_id="SHIP-001")
        await session.commit()
        await _expect_integrity_error(session, create_mock_shipment(session, order_id="ORD-002", shipment_id="SHIP-001"))
        await _expect_integrity_error(session, create_mock_shipment(session, order_id="ORD-001", shipment_id="SHIP-002"))


async def test_customer_messages_reference_valid_orders(db_session_factory):
    async with db_session_factory() as session:
        await create_mock_order(session, order_id="ORD-001")
        await create_mock_customer_message(session, order_id="ORD-001", message="Where is my parcel?")
        await create_mock_customer_message(session, order_id="ORD-001", message="Thanks!", direction="inbound")
        await session.commit()
        count = len((await session.execute(select(MockCustomerMessage))).scalars().all())
        assert count == 2  # many messages per order
        await _expect_integrity_error(
            session, create_mock_customer_message(session, order_id="ORD-404", message="hello")
        )


async def test_factories_reject_values_outside_the_approved_vocabularies(db_session_factory):
    async with db_session_factory() as session:
        with pytest.raises(ValueError):
            await create_mock_order(session, order_id="ORD-001", status="lost")
        with pytest.raises(ValueError):
            await create_mock_shipment(session, order_id="ORD-001", shipment_id="S", status="teleported")
        with pytest.raises(ValueError):
            await create_mock_customer_message(session, order_id="ORD-001", message="hi", direction="sideways")
        with pytest.raises(ValueError):
            await create_mock_customer_message(session, order_id="ORD-001", message="   ")


async def test_timezone_aware_timestamps_round_trip(db_session_factory):
    written = datetime(2026, 9, 20, 16, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    async with db_session_factory() as session:
        await create_mock_order(session, order_id="ORD-001", created_at=written)
        await session.commit()
    async with db_session_factory() as session:
        order = (await session.execute(select(MockOrder).where(MockOrder.order_id == "ORD-001"))).scalar_one()
    assert order.created_at == written  # same instant
    assert order.created_at.astimezone(timezone.utc) == datetime(2026, 9, 20, 10, 30, tzinfo=timezone.utc)
    assert order.updated_at == written


async def test_updated_at_changes_when_a_row_is_updated(db_session_factory):
    past = datetime(2026, 1, 1, tzinfo=timezone.utc)
    async with db_session_factory() as session:
        await create_mock_order(session, order_id="ORD-001", created_at=past)
        await session.commit()
    async with db_session_factory() as session:
        order = (await session.execute(select(MockOrder).where(MockOrder.order_id == "ORD-001"))).scalar_one()
        order.status = "shipped"
        await session.commit()
    async with db_session_factory() as session:
        order = (await session.execute(select(MockOrder).where(MockOrder.order_id == "ORD-001"))).scalar_one()
    assert order.created_at == past
    assert order.updated_at > past
