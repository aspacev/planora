"""document intake

Creating a project from a requirements document.

project_documents — the uploaded file and its extracted text, kept with the
project it produced. ai_sessions gains what the document path needs between
steps: the project type, the document, how far it has been read, the extracted
requirements, a short description and the interview topics the document already
answers. projects.project_type records the type the intake was run with.

Existing sessions get the type "general" — the list of topics they were already
using — and carry on unchanged. Existing projects get no type: nobody was asked.

Revision ID: a9c4e7f2b5d8
Revises: f4b8d2c6a1e9
Create Date: 2026-11-13 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a9c4e7f2b5d8'
down_revision: Union[str, Sequence[str], None] = 'f4b8d2c6a1e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'project_documents',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('org_id', sa.UUID(), nullable=False),
        sa.Column('project_id', sa.UUID(), nullable=True),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('content_type', sa.String(length=100), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('content', sa.LargeBinary(), nullable=False),
        sa.Column('extracted_text', sa.Text(), nullable=False),
        sa.Column('uploaded_by', sa.UUID(), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['uploaded_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('project_id'),
    )
    op.create_index(
        op.f('ix_project_documents_org_id'), 'project_documents', ['org_id'], unique=False
    )

    # The server defaults give the value to the existing rows at the moment of
    # ADD COLUMN and stay in the schema for a write path around the ORM.
    op.add_column(
        'ai_sessions',
        sa.Column(
            'project_type', sa.String(length=16), nullable=False,
            server_default=sa.text("'general'"),
        ),
    )
    op.add_column('ai_sessions', sa.Column('document_id', sa.UUID(), nullable=True))
    op.create_foreign_key(
        'ai_sessions_document_id_fkey', 'ai_sessions', 'project_documents',
        ['document_id'], ['id'], ondelete='SET NULL',
    )
    op.add_column(
        'ai_sessions',
        sa.Column('document_cursor', sa.Integer(), nullable=False, server_default=sa.text('0')),
    )
    op.add_column(
        'ai_sessions',
        sa.Column(
            'requirements', postgresql.JSONB(astext_type=sa.Text()), nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        'ai_sessions',
        sa.Column('brief', sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        'ai_sessions',
        sa.Column(
            'document_topics', postgresql.JSONB(astext_type=sa.Text()), nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    op.add_column('projects', sa.Column('project_type', sa.String(length=16), nullable=True))
    op.create_check_constraint(
        'ck_projects_project_type', 'projects',
        "project_type IN ('software', 'event', 'general')",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('ck_projects_project_type', 'projects', type_='check')
    op.drop_column('projects', 'project_type')

    op.drop_column('ai_sessions', 'document_topics')
    op.drop_column('ai_sessions', 'brief')
    op.drop_column('ai_sessions', 'requirements')
    op.drop_column('ai_sessions', 'document_cursor')
    op.drop_constraint('ai_sessions_document_id_fkey', 'ai_sessions', type_='foreignkey')
    op.drop_column('ai_sessions', 'document_id')
    op.drop_column('ai_sessions', 'project_type')

    op.drop_index(op.f('ix_project_documents_org_id'), table_name='project_documents')
    op.drop_table('project_documents')
