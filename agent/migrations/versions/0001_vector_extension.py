"""enable pgvector extension

The one piece of schema we own. mem0 creates its own vector table on first use
but requires the `vector` extension to already exist — this makes that bootstrap
versioned, repeatable, and applied identically to every environment instead of a
manual `gcloud sql connect ... CREATE EXTENSION`.

Revision ID: 0001
Revises:
Create Date: 2026-10-10
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    # Best-effort: will fail if mem0's vector columns still depend on it.
    op.execute("DROP EXTENSION IF EXISTS vector")
