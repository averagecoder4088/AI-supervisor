"""Static/structural tests for the SQLAlchemy ORM models.

These do not require a live PostgreSQL instance: they verify that the
models import cleanly, that all 8 core tables register on the shared
metadata, and that the relationships/constraints described in the
architecture (one Run per order, one FinalOutput per Run, cascades)
are wired correctly at the mapping level.
"""

from app.db.base import Base
from app.db.models import (
    Action,
    Event,
    FinalOutput,
    MemorySnapshot,
    Run,
    Supervisor,
    TimelineEntry,
    ToolExecution,
)

EXPECTED_TABLES = {
    "supervisors",
    "runs",
    "events",
    "timeline_entries",
    "actions",
    "tool_executions",
    "memory_snapshots",
    "final_outputs",
}


def test_exactly_the_eight_core_tables_are_registered():
    """No extra tables (e.g. a workflows/tools/orders table) should exist."""
    table_names = set(Base.metadata.tables.keys())
    assert table_names == EXPECTED_TABLES


def test_run_has_no_workflow_id_column():
    """workflow_id is derived (f'order-{order_id}'), never persisted."""
    assert "workflow_id" not in Run.__table__.columns


def test_runs_order_id_is_unique():
    unique_cols = {col.name for constraint in Run.__table__.constraints
                   for col in getattr(constraint, "columns", [])
                   if constraint.__class__.__name__ == "UniqueConstraint"}
    assert "order_id" in unique_cols or Run.__table__.columns["order_id"].unique


def test_final_output_run_id_is_unique_one_to_one():
    assert FinalOutput.__table__.columns["run_id"].unique is True


def test_supervisor_versioning_unique_constraint():
    constraint_columns = [
        {col.name for col in constraint.columns}
        for constraint in Supervisor.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    ]
    assert {"name", "version"} in constraint_columns


def test_run_relationships_cover_all_child_tables():
    mapper = Run.__mapper__
    relationship_targets = {rel.mapper.class_ for rel in mapper.relationships}
    assert relationship_targets == {
        Supervisor,
        Event,
        TimelineEntry,
        Action,
        MemorySnapshot,
        FinalOutput,
    }


def test_action_to_tool_execution_is_one_to_many():
    mapper = Action.__mapper__
    rel = mapper.relationships["tool_executions"]
    assert rel.uselist is True
    assert rel.mapper.class_ is ToolExecution


def test_foreign_key_cascade_behavior():
    # Child records cascade-delete with their run; supervisors are protected
    # from deletion while runs reference them (RESTRICT), per the architecture's
    # "PostgreSQL is the application/history database" boundary.
    run_supervisor_fk = next(iter(Run.__table__.columns["supervisor_id"].foreign_keys))
    assert run_supervisor_fk.ondelete == "RESTRICT"

    for table, column in [
        (Event, "run_id"),
        (TimelineEntry, "run_id"),
        (Action, "run_id"),
        (MemorySnapshot, "run_id"),
        (FinalOutput, "run_id"),
    ]:
        fk = next(iter(table.__table__.columns[column].foreign_keys))
        assert fk.ondelete == "CASCADE"

    tool_execution_fk = next(iter(ToolExecution.__table__.columns["action_id"].foreign_keys))
    assert tool_execution_fk.ondelete == "CASCADE"
