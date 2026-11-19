"""Use precise amounts in the pre-membership identity operation ledger."""

import sqlalchemy as sa

from alembic import op

revision = "r0002a_identity_cost"
down_revision = "r0002_identity_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "identity_provider_events",
        "cost_amount",
        type_=sa.Numeric(38, 12),
        postgresql_using="cost_amount::numeric(38,12)",
    )


def downgrade() -> None:
    op.alter_column("identity_provider_events", "cost_amount", type_=sa.String())
