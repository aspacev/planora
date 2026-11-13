"""project undo floor

projects.undo_floor_seq — the number of the last revision that "Undo" no longer
reaches. Assigning or moving the start date lays every task out on a new axis
without going through the journal, while the inverse operations of the earlier
revisions still hold dates of the old one: undoing "moved to day 8" after the
plan was anchored to March put the task back on 1 January 2001 — the epoch of
the relative axis. The floor is raised to the head of the journal whenever the
axis changes, and everything at or below it stays in the history as a record
but is no longer offered for undoing.

There is no backfill: which revisions of an already anchored project predate its
anchoring is not recorded anywhere. Zero means "nothing is sealed" — the
behaviour before this column.

Revision ID: e7a3c9b1d4f2
Revises: d5e8f1a2b3c4
Create Date: 2026-11-13 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7a3c9b1d4f2'
down_revision: Union[str, Sequence[str], None] = 'd5e8f1a2b3c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'projects',
        sa.Column('undo_floor_seq', sa.Integer(), nullable=False, server_default=sa.text('0')),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('projects', 'undo_floor_seq')
