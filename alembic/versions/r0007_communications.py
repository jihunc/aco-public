"""Additive communications and simulated native ledger; history-safe downgrade."""

from sqlalchemy import text

from alembic import op

revision = "r0007_communications"
down_revision = "r0006_artifacts"
branch_labels = None
depends_on = None
DOMAIN_DDL = (
    """
CREATE TABLE demo_email_invocations (
	tenant_id UUID NOT NULL,
	provider_connection_id UUID NOT NULL,
	operation_id UUID NOT NULL,
	method VARCHAR NOT NULL,
	payload_hash VARCHAR NOT NULL,
	native_request_id VARCHAR NOT NULL,
	result VARCHAR NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(tenant_id, provider_connection_id) REFERENCES provider_connections (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
)

""",
    """CREATE INDEX ix_demo_email_invocations_tenant_id ON demo_email_invocations (tenant_id)""",
    """
CREATE TABLE demo_email_operations (
	tenant_id UUID NOT NULL,
	provider_connection_id UUID NOT NULL,
	operation_id UUID NOT NULL,
	generation INTEGER NOT NULL,
	payload_hash VARCHAR NOT NULL,
	native_message_id VARCHAR NOT NULL,
	native_request_id VARCHAR NOT NULL,
	vendor_key_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	message JSON NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, provider_connection_id, operation_id, generation),
	FOREIGN KEY(tenant_id, provider_connection_id) REFERENCES provider_connections (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
)

""",
    """CREATE INDEX ix_demo_email_operations_tenant_id ON demo_email_operations (tenant_id)""",
    """
CREATE TABLE email_execution_activations (
	tenant_id UUID NOT NULL,
	email_connection_id UUID NOT NULL,
	email_connection_version INTEGER NOT NULL,
	storage_connection_id UUID NOT NULL,
	storage_connection_version INTEGER NOT NULL,
	reviewer_id UUID NOT NULL,
	membership_version INTEGER NOT NULL,
	sender VARCHAR NOT NULL,
	destinations JSON NOT NULL,
	request_cap INTEGER NOT NULL,
	capture_policy VARCHAR NOT NULL,
	evidence VARCHAR NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id, email_connection_id) REFERENCES provider_connections (tenant_id, id),
	FOREIGN KEY(tenant_id, storage_connection_id) REFERENCES provider_connections (tenant_id, id),
	FOREIGN KEY(tenant_id, reviewer_id) REFERENCES memberships (tenant_id, user_id),
	CHECK (request_cap > 0 AND request_cap <= 100),
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
)

""",
    """CREATE INDEX ix_email_execution_activations_tenant_id ON email_execution_activations (tenant_id)""",
    """
CREATE TABLE email_activation_revocations (
	tenant_id UUID NOT NULL,
	activation_id UUID NOT NULL,
	actor_id UUID NOT NULL,
	reason VARCHAR NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, activation_id),
	FOREIGN KEY(tenant_id, activation_id) REFERENCES email_execution_activations (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES tenants (id),
	FOREIGN KEY(actor_id) REFERENCES identity_actors (id)
)

""",
    """CREATE INDEX ix_email_activation_revocations_tenant_id ON email_activation_revocations (tenant_id)""",
    """
CREATE TABLE communication_threads (
	tenant_id UUID NOT NULL,
	provider_connection_id UUID NOT NULL,
	origin_invoice_id UUID NOT NULL,
	customer_id UUID NOT NULL,
	native_thread_id VARCHAR,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	UNIQUE (tenant_id, provider_connection_id, origin_invoice_id),
	UNIQUE (tenant_id, provider_connection_id, native_thread_id),
	FOREIGN KEY(tenant_id, provider_connection_id) REFERENCES provider_connections (tenant_id, id),
	FOREIGN KEY(tenant_id, origin_invoice_id, customer_id)
        REFERENCES invoice_snapshots (tenant_id, id, customer_id),
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
)

""",
    """CREATE INDEX ix_communication_threads_tenant_id ON communication_threads (tenant_id)""",
    """
CREATE TABLE communications (
	tenant_id UUID NOT NULL,
	provider_connection_id UUID NOT NULL,
	thread_id UUID NOT NULL,
	origin_invoice_id UUID NOT NULL,
	operation_id UUID NOT NULL,
	native_message_id VARCHAR,
	direction VARCHAR NOT NULL,
	channel VARCHAR NOT NULL,
	kind VARCHAR NOT NULL,
	synthetic BOOLEAN NOT NULL,
	content JSON NOT NULL,
	event_at TIMESTAMP WITH TIME ZONE NOT NULL,
	delivery_state VARCHAR NOT NULL,
	source_state VARCHAR NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	UNIQUE (tenant_id, operation_id),
	UNIQUE (tenant_id, provider_connection_id, native_message_id),
	FOREIGN KEY(tenant_id, provider_connection_id) REFERENCES provider_connections (tenant_id, id),
	FOREIGN KEY(tenant_id, thread_id) REFERENCES communication_threads (tenant_id, id),
	FOREIGN KEY(tenant_id, origin_invoice_id) REFERENCES invoice_snapshots (tenant_id, id),
	CHECK (direction IN ('incoming','outgoing')),
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
)

""",
    """CREATE INDEX ix_communications_tenant_id ON communications (tenant_id)""",
    """
CREATE TABLE communication_delivery_events (
	tenant_id UUID NOT NULL,
	communication_id UUID NOT NULL,
	provider_connection_id UUID NOT NULL,
	event_key VARCHAR NOT NULL,
	state VARCHAR NOT NULL,
	occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
	evidence JSON NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, provider_connection_id, event_key),
	FOREIGN KEY(tenant_id, communication_id) REFERENCES communications (tenant_id, id),
	FOREIGN KEY(tenant_id, provider_connection_id) REFERENCES provider_connections (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
)

""",
    """CREATE INDEX ix_communication_delivery_events_tenant_id
        ON communication_delivery_events (tenant_id)""",
    """
CREATE TABLE communication_invoice_links (
	tenant_id UUID NOT NULL,
	communication_id UUID NOT NULL,
	invoice_id UUID NOT NULL,
	claim_or_context_key VARCHAR NOT NULL,
	provenance VARCHAR NOT NULL,
	confidence VARCHAR NOT NULL,
	evidence_excerpt VARCHAR NOT NULL,
	evidence_offset INTEGER NOT NULL,
	classification VARCHAR NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, communication_id, invoice_id, claim_or_context_key),
	FOREIGN KEY(tenant_id, communication_id) REFERENCES communications (tenant_id, id),
	FOREIGN KEY(tenant_id, invoice_id) REFERENCES invoice_snapshots (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
)

""",
    """CREATE INDEX ix_communication_invoice_links_tenant_id ON communication_invoice_links (tenant_id)""",
    """
CREATE TABLE communication_sources (
	tenant_id UUID NOT NULL,
	communication_id UUID NOT NULL,
	artifact_id UUID NOT NULL,
	representation VARCHAR NOT NULL,
	sha256 VARCHAR NOT NULL,
	evidence JSON NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, communication_id, representation, sha256),
	FOREIGN KEY(tenant_id, communication_id) REFERENCES communications (tenant_id, id),
	FOREIGN KEY(tenant_id, artifact_id) REFERENCES artifacts (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
)

""",
    """CREATE INDEX ix_communication_sources_tenant_id ON communication_sources (tenant_id)""",
    """
CREATE TABLE email_dispatch_bindings (
	tenant_id UUID NOT NULL,
	action_id UUID NOT NULL,
	communication_id UUID NOT NULL,
	activation_id UUID NOT NULL,
	email_connection_id UUID NOT NULL,
	storage_connection_id UUID NOT NULL,
	operation_id UUID NOT NULL,
	operation_key VARCHAR NOT NULL,
	payload_hash VARCHAR NOT NULL,
	source_revision VARCHAR NOT NULL,
	command JSON NOT NULL,
	mime_boundary VARCHAR NOT NULL,
	mime_sha256 VARCHAR NOT NULL,
	contact_reservation_id UUID NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	UNIQUE (tenant_id, action_id),
	UNIQUE (tenant_id, operation_id),
	FOREIGN KEY(tenant_id, action_id) REFERENCES scheduled_actions (tenant_id, id),
	FOREIGN KEY(tenant_id, communication_id) REFERENCES communications (tenant_id, id),
	FOREIGN KEY(tenant_id, activation_id) REFERENCES email_execution_activations (tenant_id, id),
	FOREIGN KEY(tenant_id, email_connection_id) REFERENCES provider_connections (tenant_id, id),
	FOREIGN KEY(tenant_id, storage_connection_id) REFERENCES provider_connections (tenant_id, id),
	FOREIGN KEY(tenant_id, contact_reservation_id) REFERENCES contact_reservations (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES tenants (id),
	FOREIGN KEY(contact_reservation_id) REFERENCES contact_reservations (id)
)

""",
    """CREATE INDEX ix_email_dispatch_bindings_tenant_id ON email_dispatch_bindings (tenant_id)""",
    """
CREATE TABLE communication_capture_grants (
	tenant_id UUID NOT NULL,
	actor_id UUID NOT NULL,
	actor_version INTEGER NOT NULL,
	tenant_generation INTEGER NOT NULL,
	binding_id UUID NOT NULL,
	communication_id UUID NOT NULL,
	storage_connection_id UUID NOT NULL,
	correlation_id UUID NOT NULL,
	action_version INTEGER NOT NULL,
	job_id INTEGER NOT NULL,
	lease_token UUID NOT NULL,
	source_revision VARCHAR NOT NULL,
	objects JSON NOT NULL,
	capture_policy VARCHAR NOT NULL,
	issued_at TIMESTAMP WITH TIME ZONE NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	FOREIGN KEY(tenant_id, binding_id) REFERENCES email_dispatch_bindings (tenant_id, id),
	FOREIGN KEY(tenant_id, communication_id) REFERENCES communications (tenant_id, id),
	FOREIGN KEY(tenant_id, actor_id) REFERENCES identity_actors (tenant_id, id),
	FOREIGN KEY(tenant_id, storage_connection_id) REFERENCES provider_connections (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
)

""",
    """CREATE INDEX ix_communication_capture_grants_tenant_id ON communication_capture_grants (tenant_id)""",
    """
CREATE TABLE email_submissions (
	tenant_id UUID NOT NULL,
	binding_id UUID NOT NULL,
	operation_id UUID NOT NULL,
	state VARCHAR NOT NULL,
	intent_at TIMESTAMP WITH TIME ZONE,
	provider_message_id VARCHAR,
	provider_request_id VARCHAR,
	failure JSON,
	usage_attempt_id UUID,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, id),
	UNIQUE (tenant_id, operation_id),
	FOREIGN KEY(tenant_id, binding_id) REFERENCES email_dispatch_bindings (tenant_id, id),
	FOREIGN KEY(tenant_id, usage_attempt_id) REFERENCES usage_attempts (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES tenants (id),
	FOREIGN KEY(usage_attempt_id) REFERENCES usage_attempts (id)
)

""",
    """CREATE INDEX ix_email_submissions_tenant_id ON email_submissions (tenant_id)""",
    """
CREATE TABLE capture_grant_revocations (
	tenant_id UUID NOT NULL,
	grant_id UUID NOT NULL,
	reason VARCHAR NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, grant_id),
	FOREIGN KEY(tenant_id, grant_id) REFERENCES communication_capture_grants (tenant_id, id),
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
)

""",
    """CREATE INDEX ix_capture_grant_revocations_tenant_id ON capture_grant_revocations (tenant_id)""",
)
TABLES = (
    "demo_email_invocations",
    "demo_email_operations",
    "email_execution_activations",
    "email_activation_revocations",
    "communication_threads",
    "communications",
    "communication_delivery_events",
    "communication_invoice_links",
    "communication_sources",
    "email_dispatch_bindings",
    "communication_capture_grants",
    "email_submissions",
    "capture_grant_revocations",
)
IMMUTABLE = (
    "email_execution_activations",
    "email_activation_revocations",
    "email_dispatch_bindings",
    "communication_capture_grants",
    "capture_grant_revocations",
    "communication_invoice_links",
    "communication_sources",
    "communication_delivery_events",
    "demo_email_invocations",
)


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_contact_reservations_tenant_id", "contact_reservations", ["tenant_id", "id"]
    )
    op.create_unique_constraint("uq_usage_attempts_tenant_id", "usage_attempts", ["tenant_id", "id"])
    for statement in DOMAIN_DDL:
        op.execute(statement)
    op.execute(
        "CREATE FUNCTION aco_communication_immutable() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN RAISE EXCEPTION 'immutable communication evidence'; END $$"
    )
    for name in IMMUTABLE:
        op.execute(
            f"CREATE TRIGGER immutable_{name} BEFORE UPDATE OR DELETE ON {name} "
            "FOR EACH ROW EXECUTE FUNCTION aco_communication_immutable()"
        )


def downgrade() -> None:
    for name in TABLES:
        if op.get_bind().execute(text(f"SELECT EXISTS (SELECT 1 FROM {name})")).scalar():
            raise RuntimeError("T10 history cannot be represented by r0006; downgrade refused")
    for name in reversed(TABLES):
        op.drop_table(name)
    op.execute("DROP FUNCTION aco_communication_immutable()")
    op.drop_constraint("uq_contact_reservations_tenant_id", "contact_reservations", type_="unique")
    op.drop_constraint("uq_usage_attempts_tenant_id", "usage_attempts", type_="unique")
