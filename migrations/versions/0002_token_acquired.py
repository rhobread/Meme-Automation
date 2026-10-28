"""Track long-lived token acquisition for safe refresh eligibility."""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("instagram_accounts", sa.Column("token_acquired_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("instagram_accounts", "token_acquired_at")
