"""SQLAlchemy ORM models for the Order Supervisor application database.

These map directly onto the 8 core entities fixed by the Final
Architecture Specification: supervisors, runs, events, timeline_entries,
actions, tool_executions, memory_snapshots, final_outputs.

PostgreSQL is the application/history database (this module). Temporal
owns durable workflow execution state — it is not modeled here. The
Temporal workflow ID is intentionally NOT stored: it is deterministic
(``f"order-{order_id}"``) and derived wherever needed instead of
persisted, per the architecture decision.

Field/type choices (UUID PKs, JSONB columns, plain String status
columns, indexes) are implementation-level decisions within the frozen
architecture, not architectural decisions themselves.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db.base import Base


class Supervisor(Base):
    """Reusable, versioned supervisor configuration/template."""

    __tablename__ = "supervisors"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_supervisor_name_version"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)

    # Event types that immediately trigger reasoning, e.g. ["shipment_delayed", "refund_requested"].
    wake_policy: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Enabled tool names, e.g. ["get_order_status", "escalate_shipment"].
    enabled_tools: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    default_wake_interval: Mapped[int] = mapped_column(nullable=False)
    min_wake_interval: Mapped[int] = mapped_column(nullable=False)
    max_wake_interval: Mapped[int] = mapped_column(nullable=False)

    # Order statuses that end supervision, e.g. ["delivered"].
    terminal_order_statuses: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # Configured event_type -> order_status updates, e.g. {"delivered": "delivered"}.
    # Versioned with the supervisor; no mapping is built into the code (Step 4, B3).
    order_status_by_event: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    version: Mapped[int] = mapped_column(nullable=False, default=1)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(), onupdate=func.now(), nullable=False
    )

    runs: Mapped[list["Run"]] = relationship(back_populates="supervisor")


class Run(Base):
    """One actual order-supervision run (order_id <-> one Temporal OrderWorkflow)."""

    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[str] = mapped_column(nullable=False, unique=True, index=True)
    supervisor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("supervisors.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    # Application-level run lifecycle. NOT Temporal's live operational state
    # (sleeping/reasoning/paused/waiting), which Temporal owns.
    status: Mapped[str] = mapped_column(nullable=False, default="active")
    # Business order status, e.g. "shipment_delayed". Distinct from `status` above.
    order_status: Mapped[Optional[str]] = mapped_column(nullable=True)
    # Run-specific human instructions, e.g. [{"text": "...", "added_at": "..."}].
    # A property of the run, not timeline history (Step 4, B2).
    run_instructions: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    supervisor: Mapped["Supervisor"] = relationship(back_populates="runs")
    events: Mapped[list["Event"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    timeline_entries: Mapped[list["TimelineEntry"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    actions: Mapped[list["Action"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    memory_snapshots: Mapped[list["MemorySnapshot"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    final_output: Mapped[Optional["FinalOutput"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", uselist=False
    )


class Event(Base):
    """Raw incoming order event (recorded regardless of whether it wakes reasoning)."""

    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    run: Mapped["Run"] = relationship(back_populates="events")


class TimelineEntry(Base):
    """Human-readable chronological history entry for a run (broader than raw events)."""

    __tablename__ = "timeline_entries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # e.g. "event" / "action" / "system". Application-constrained, not a DB enum.
    entry_type: Mapped[str] = mapped_column(nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    run: Mapped["Run"] = relationship(back_populates="timeline_entries")


class Action(Base):
    """A supervisor decision (what the AI decided to do)."""

    __tablename__ = "actions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    action_type: Mapped[str] = mapped_column(nullable=False)
    # "pending" | "completed" | "failed" — application-constrained, not a DB enum.
    status: Mapped[str] = mapped_column(nullable=False, default="pending")
    reasoning: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped["Run"] = relationship(back_populates="actions")
    tool_executions: Mapped[list["ToolExecution"]] = relationship(
        back_populates="action", cascade="all, delete-orphan"
    )


class ToolExecution(Base):
    """What actually happened when a tool associated with an Action was executed."""

    __tablename__ = "tool_executions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    action_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tool_name: Mapped[str] = mapped_column(nullable=False)
    # "pending" | "success" | "failed" — application-constrained, not a DB enum.
    status: Mapped[str] = mapped_column(nullable=False, default="pending")
    # Structured tool call record (Step 4, B4). The row id doubles as the
    # tool's idempotency key.
    input: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    result: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    action: Mapped["Action"] = relationship(back_populates="tool_executions")


class MemorySnapshot(Base):
    """Compact, evolving supervisor memory captured after a reasoning cycle."""

    __tablename__ = "memory_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    memory: Mapped[dict] = mapped_column(JSONB, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    run: Mapped["Run"] = relationship(back_populates="memory_snapshots")


class FinalOutput(Base):
    """Terminal result generated when a run reaches a configured terminal order status."""

    __tablename__ = "final_outputs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    output: Mapped[dict] = mapped_column(JSONB, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    run: Mapped["Run"] = relationship(back_populates="final_output")
