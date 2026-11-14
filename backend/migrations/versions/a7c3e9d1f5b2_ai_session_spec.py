"""ai session spec

ai_sessions.spec — the text of an uploaded specification the intake starts from.

On the session rather than in a table of its own: a specification belongs to
exactly one session, is read whole on every model call of that session and is
never queried by itself.

An empty string rather than NULL means "started without a specification": every
reader asks "is there text", and a nullable column would give that question two
negative answers.

The server_default stays in the schema: the rows that exist get the value at the
moment of ADD COLUMN, and an INSERT around the ORM does not break on a NOT NULL
with no default — by the same reasoning as with projects.auto_schedule.

Revision ID: a7c3e9d1f5b2
Revises: d5e8f1a2b3c4
Create Date: 2026-11-14 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c3e9d1f5b2'
down_revision: Union[str, Sequence[str], None] = 'd5e8f1a2b3c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'ai_sessions',
        sa.Column('spec', sa.Text(), nullable=False, server_default=sa.text("''")),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('ai_sessions', 'spec')
