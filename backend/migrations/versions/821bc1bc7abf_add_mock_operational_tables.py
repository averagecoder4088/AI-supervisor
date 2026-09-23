"""add mock operational tables

Revision ID: 821bc1bc7abf
Revises: ae4b8dc1c74f
Create Date: 2026-09-24

Step 5: additive-only. Creates the mock operational environment the four
existing tools read and mutate (see app/db/mock_models.py):

- mock_orders             (order_id UNIQUE)
- mock_shipments          (shipment_id UNIQUE, order_id UNIQUE FK -> mock_orders.order_id)
- mock_customer_messages  (order_id FK -> mock_orders.order_id, indexed)

No existing table is touched. No data is seeded.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '821bc1bc7abf'
down_revision: Union[str, Sequence[str], None] = 'ae4b8dc1c74f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "mock_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("order_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("customer_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("order_id", name="uq_mock_orders_order_id"),
    )

    op.create_table(
        "mock_shipments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "order_id",
            sa.String(),
            sa.ForeignKey("mock_orders.order_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("shipment_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("tracking_number", sa.String(), nullable=False),
        sa.Column("delay_reason", sa.Text(), nullable=True),
        sa.Column("escalated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("shipment_id", name="uq_mock_shipments_shipment_id"),
        # One shipment per order for the POC.
        sa.UniqueConstraint("order_id", name="uq_mock_shipments_order_id"),
    )
    op.create_index("ix_mock_shipments_order_id", "mock_shipments", ["order_id"])

    op.create_table(
        "mock_customer_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "order_id",
            sa.String(),
            sa.ForeignKey("mock_orders.order_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("direction", sa.String(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_mock_customer_messages_order_id", "mock_customer_messages", ["order_id"])


def downgrade() -> None:
    """Downgrade schema: drops only the three mock tables."""
    op.drop_index("ix_mock_customer_messages_order_id", table_name="mock_customer_messages")
    op.drop_table("mock_customer_messages")
    op.drop_index("ix_mock_shipments_order_id", table_name="mock_shipments")
    op.drop_table("mock_shipments")
    op.drop_table("mock_orders")
