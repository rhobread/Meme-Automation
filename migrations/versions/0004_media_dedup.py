"""Enforce exact-image deduplication within each account.

Revision ID: 0004
Revises: 0003
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("uq_posts_account_media_hash", "content_posts", ["account_id", "media_hash"], unique=True)


def downgrade():
    op.drop_index("uq_posts_account_media_hash", table_name="content_posts")
