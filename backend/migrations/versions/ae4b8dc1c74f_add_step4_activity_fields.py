"""add step 4 activity fields

Revision ID: ae4b8dc1c74f
Revises: da039b3edfdf
Create Date: 2026-09-24

Additive-only migration for the approved Step 4 decisions:

- B2: runs.run_instructions            JSONB NOT NULL DEFAULT '[]'
- B3: supervisors.order_status_by_event JSONB NOT NULL DEFAULT '{}'
- B4: tool_executions.input            JSONB NOT NULL DEFAULT '{}'
      tool_executions.result           JSONB NULL
      tool_executions.error            TEXT  NULL

Every new NOT NULL column has a server default, so existing rows stay valid.
No existing column, constraint, index or row is changed or dropped.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'ae4b8dc1c74f'
down_revision: Union[str, Sequence[str], None] = 'da039b3edfdf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "runs",
        sa.Column(
            "run_instructions",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "supervisors",
        sa.Column(
            "order_status_by_event",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "tool_executions",
        sa.Column(
            "input",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column("tool_executions", sa.Column("result", postgresql.JSONB(), nullable=True))
    op.add_column("tool_executions", sa.Column("error", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema: drops only the columns added above."""
    op.drop_column("tool_executions", "error")
    op.drop_column("tool_executions", "result")
    op.drop_column("tool_executions", "input")
    op.drop_column("supervisors", "order_status_by_event")
    op.drop_column("runs", "run_instructions")
