"""drop jira integration

The Jira integration is removed from the product: the connection settings, the
import of a project and the sync both ways. Its four tables go with it — an
organization's connection (with the encrypted API token) and the three link
tables that tied projects, stages and tasks to Jira issues.

The projects, stages and tasks that were imported stay exactly as they are:
they were created as ordinary plan rows, and only the bookkeeping that pointed
back at Jira is deleted.

The downgrade recreates the tables empty, in their last shape (with
`pushed_due_date`): the stored tokens and links are not recoverable.

Revision ID: f4b8d2c6a1e9
Revises: e7a3c9b1d4f2
Create Date: 2026-11-13 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4b8d2c6a1e9'
down_revision: Union[str, Sequence[str], None] = 'e7a3c9b1d4f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index(op.f('ix_jira_task_links_project_id'), table_name='jira_task_links')
    op.drop_table('jira_task_links')
    op.drop_index(op.f('ix_jira_category_links_project_id'), table_name='jira_category_links')
    op.drop_table('jira_category_links')
    op.drop_table('jira_project_links')
    op.drop_table('jira_connections')


def downgrade() -> None:
    """Downgrade schema."""
    op.create_table(
        'jira_connections',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('org_id', sa.UUID(), nullable=False),
        sa.Column('base_url', sa.String(length=300), nullable=False),
        sa.Column('email', sa.String(length=320), nullable=False),
        sa.Column('encrypted_token', sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('org_id'),
    )

    op.create_table(
        'jira_project_links',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('project_id', sa.UUID(), nullable=False),
        sa.Column('jira_project_key', sa.String(length=64), nullable=False),
        sa.Column('jql', sa.Text(), nullable=False),
        sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('project_id'),
    )

    op.create_table(
        'jira_category_links',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('project_id', sa.UUID(), nullable=False),
        sa.Column('category_id', sa.UUID(), nullable=False),
        sa.Column('issue_key', sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(['category_id'], ['categories.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('category_id'),
        sa.UniqueConstraint('project_id', 'issue_key'),
    )
    op.create_index(
        op.f('ix_jira_category_links_project_id'), 'jira_category_links', ['project_id'],
        unique=False,
    )

    op.create_table(
        'jira_task_links',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('project_id', sa.UUID(), nullable=False),
        sa.Column('task_id', sa.UUID(), nullable=True),
        sa.Column('issue_key', sa.String(length=64), nullable=False),
        sa.Column('pushed_due_date', sa.Date(), nullable=True),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('project_id', 'issue_key'),
        sa.UniqueConstraint('task_id'),
    )
    op.create_index(
        op.f('ix_jira_task_links_project_id'), 'jira_task_links', ['project_id'],
        unique=False,
    )
