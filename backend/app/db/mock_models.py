"""Mock OPERATIONAL state (Step 5): the simulated "real world" the tools act on.

This is deliberately separate from the supervisor/history tables in
``models.py``. A supervisor ``tool_executions`` row means "the supervisor
invoked this operation"; these tables hold the operation's resulting
operational state (an order's status, a shipment's escalation flag, a
message that was "sent").

The tables use their own declarative base (``MockBase``) so the two concerns
never share metadata: the supervisor schema stays exactly the eight core
tables, and nothing here can be mistaken for supervisor history.

Status/direction columns are plain strings constrained by the application
(same convention as Step 2): see the vocabularies below.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func

# Approved vocabularies (S5-D5, S5-D6).
ORDER_STATUSES = frozenset(
    {
        "created",
        "payment_confirmed",
        "payment_failed",
        "shipped",
        "delayed",
        "delivered",
        "cancelled",
        "refund_requested",
    }
)
SHIPMENT_STATUSES = frozenset({"created", "in_transit", "delayed", "delivered", "cancelled"})
MESSAGE_DIRECTIONS = frozenset({"inbound", "outbound"})


class MockBase(DeclarativeBase):
    """Declarative base for the mock operational tables only."""


class MockOrder(MockBase):
    __tablename__ = "mock_orders"
    __table_args__ = (UniqueConstraint("order_id", name="uq_mock_orders_order_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[str] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(nullable=False)
    customer_id: Mapped[str] = mapped_column(nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class MockShipment(MockBase):
    """One shipment per order for the POC (``order_id`` is unique)."""

    __tablename__ = "mock_shipments"
    __table_args__ = (
        UniqueConstraint("shipment_id", name="uq_mock_shipments_shipment_id"),
        UniqueConstraint("order_id", name="uq_mock_shipments_order_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[str] = mapped_column(
        ForeignKey("mock_orders.order_id", ondelete="CASCADE"), nullable=False, index=True
    )
    shipment_id: Mapped[str] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(nullable=False)
    tracking_number: Mapped[str] = mapped_column(nullable=False)
    delay_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    escalated: Mapped[bool] = mapped_column(nullable=False, default=False, server_default=text("false"))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class MockCustomerMessage(MockBase):
    __tablename__ = "mock_customer_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[str] = mapped_column(
        ForeignKey("mock_orders.order_id", ondelete="CASCADE"), nullable=False, index=True
    )
    # "inbound" | "outbound"
    direction: Mapped[str] = mapped_column(nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
