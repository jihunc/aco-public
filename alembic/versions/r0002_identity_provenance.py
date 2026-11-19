"""Identity operation provenance without invented cost or tenant authority."""

from alembic import op

revision = "r0002_identity_provenance"
down_revision = "r0001_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE identity_provider_events (
	connection_id UUID NOT NULL,
	correlation_id UUID NOT NULL,
	operation VARCHAR NOT NULL,
	provider VARCHAR NOT NULL,
	native_id VARCHAR,
	outcome VARCHAR NOT NULL,
	attempt INTEGER NOT NULL,
	cost_quality VARCHAR NOT NULL,
	cost_amount VARCHAR,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id)
)""")


def downgrade() -> None:
    op.drop_table("identity_provider_events")
