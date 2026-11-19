"""Frozen initial identity schema; no prior compatibility table is altered."""

from alembic import op

revision = "r0001_identity"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE identity_login_attempts (
	state VARCHAR NOT NULL,
	encrypted_payload VARCHAR NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	consumed BOOLEAN NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (state)
)""")
    op.execute("""CREATE TABLE identity_users (
	issuer VARCHAR(512) NOT NULL,
	subject VARCHAR(512) NOT NULL,
	email VARCHAR(320) NOT NULL,
	email_verified BOOLEAN NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (issuer, subject)
)""")
    op.execute("""CREATE TABLE tenants (
	name VARCHAR(120) NOT NULL,
	timezone VARCHAR(80) NOT NULL,
	is_demo BOOLEAN NOT NULL,
	automation_enabled BOOLEAN NOT NULL,
	outbound_paused BOOLEAN NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id)
)""")
    op.execute("""CREATE TABLE identity_audit_events (
	tenant_id UUID NOT NULL,
	actor_id UUID NOT NULL,
	correlation_id UUID NOT NULL,
	action VARCHAR NOT NULL,
	target_id UUID NOT NULL,
	details JSON NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(tenant_id) REFERENCES tenants (id),
	FOREIGN KEY(actor_id) REFERENCES identity_users (id)
)""")
    op.execute("""CREATE INDEX ix_identity_audit_events_tenant_id ON identity_audit_events (tenant_id)""")
    op.execute("""CREATE TABLE identity_invitations (
	tenant_id UUID NOT NULL,
	creator_id UUID NOT NULL,
	email VARCHAR NOT NULL,
	role VARCHAR NOT NULL,
	token_hash VARCHAR NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	used_at TIMESTAMP WITH TIME ZONE,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(tenant_id) REFERENCES tenants (id),
	FOREIGN KEY(creator_id) REFERENCES identity_users (id),
	UNIQUE (token_hash)
)""")
    op.execute("""CREATE INDEX ix_identity_invitations_tenant_id ON identity_invitations (tenant_id)""")
    op.execute("""CREATE TABLE identity_sessions (
	user_id UUID NOT NULL,
	csrf_token VARCHAR NOT NULL,
	encrypted_token VARCHAR NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	revoked BOOLEAN NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES identity_users (id)
)""")
    op.execute("""CREATE INDEX ix_identity_sessions_user_id ON identity_sessions (user_id)""")
    op.execute("""CREATE TABLE memberships (
	tenant_id UUID NOT NULL,
	user_id UUID NOT NULL,
	role VARCHAR(32) NOT NULL,
	active BOOLEAN NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, user_id),
	CHECK (role IN ('owner','finance_manager','operator','viewer')),
	FOREIGN KEY(tenant_id) REFERENCES tenants (id),
	FOREIGN KEY(user_id) REFERENCES identity_users (id)
)""")
    op.execute("""CREATE INDEX ix_memberships_tenant_id ON memberships (tenant_id)""")
    op.execute("""CREATE INDEX ix_memberships_user_id ON memberships (user_id)""")
    op.execute("""CREATE TABLE provider_connections (
	tenant_id UUID NOT NULL,
	provider_type VARCHAR(32) NOT NULL,
	provider VARCHAR(80) NOT NULL,
	external_account_id VARCHAR,
	encrypted_credentials_ref VARCHAR,
	capabilities JSON NOT NULL,
	status VARCHAR NOT NULL,
	sync_cursor VARCHAR,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
)""")
    op.execute("""CREATE INDEX ix_provider_connections_tenant_id ON provider_connections (tenant_id)""")


def downgrade() -> None:
    op.drop_table("provider_connections")
    op.drop_table("memberships")
    op.drop_table("identity_sessions")
    op.drop_table("identity_invitations")
    op.drop_table("identity_audit_events")
    op.drop_table("tenants")
    op.drop_table("identity_users")
    op.drop_table("identity_login_attempts")
