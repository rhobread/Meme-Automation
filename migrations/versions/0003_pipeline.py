"""Original-content pipeline checkpoints and metered AI requests.

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("meme_candidates", sa.Column("discovery_metadata", sa.JSON(), nullable=False, server_default="{}"))
    for name in ("concept", "top_text", "bottom_text", "alt_text"):
        op.add_column("content_posts", sa.Column(name, sa.String(), nullable=True))
    op.add_column("content_posts", sa.Column("template_type", sa.String(40), nullable=True))
    op.add_column("content_posts", sa.Column("generation_metadata", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("durable_jobs", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table("ai_usage",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("instagram_accounts.id"), nullable=False),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("durable_jobs.id"), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), sa.ForeignKey("meme_candidates.id"), nullable=False),
        sa.Column("operation", sa.String(40), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("prompt_version", sa.String(200), nullable=False),
        sa.Column("response_id", sa.String(200), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_cost_usd", sa.Float(), nullable=True),
        sa.Column("pricing", sa.JSON(), nullable=False),
        sa.CheckConstraint("input_tokens >= 0 AND cached_input_tokens >= 0 AND output_tokens >= 0"))
    op.create_index("ix_ai_usage_account_id", "ai_usage", ["account_id"])
    op.create_index("ix_ai_usage_job_id", "ai_usage", ["job_id"])


def downgrade():
    op.drop_table("ai_usage")
    op.drop_column("durable_jobs", "next_attempt_at")
    for name in ("generation_metadata", "template_type", "alt_text", "bottom_text", "top_text", "concept"):
        op.drop_column("content_posts", name)
    op.drop_column("meme_candidates", "discovery_metadata")
