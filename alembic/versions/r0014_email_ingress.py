"""Exact synthetic notification authority and unlinked inbound original preservation."""

from alembic import op

revision = "r0014_email_ingress"
down_revision = "r0013_approval_proposal_history"
branch_labels = None
depends_on = None

DDL = (
    """CREATE TABLE email_ingress_activations (
        tenant_id UUID NOT NULL,
        email_connection_id UUID NOT NULL,
        email_connection_version INTEGER NOT NULL,
        storage_connection_id UUID NOT NULL,
        storage_connection_version INTEGER NOT NULL,
        enabling_member_id UUID NOT NULL,
        membership_version INTEGER NOT NULL,
        worker_actor_id UUID NOT NULL,
        actor_version INTEGER NOT NULL,
        tenant_generation INTEGER NOT NULL,
        secret_sha256 VARCHAR NOT NULL,
        evidence VARCHAR NOT NULL,
        expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
        revoked_at TIMESTAMP WITH TIME ZONE,
        id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        version INTEGER NOT NULL,
        PRIMARY KEY (id),
        UNIQUE (tenant_id, id),
        FOREIGN KEY(tenant_id, email_connection_id) REFERENCES provider_connections (tenant_id, id),
        FOREIGN KEY(tenant_id, storage_connection_id) REFERENCES provider_connections (tenant_id,
id),
        FOREIGN KEY(tenant_id, worker_actor_id) REFERENCES identity_actors (tenant_id, id),
        FOREIGN KEY(tenant_id, enabling_member_id) REFERENCES memberships (tenant_id, user_id),
        FOREIGN KEY(tenant_id) REFERENCES tenants (id)
)""",
    """CREATE INDEX ix_email_ingress_activations_tenant_id ON email_ingress_activations (tenant_id)""",
    """CREATE TABLE email_ingress_receipts (
        tenant_id UUID NOT NULL,
        activation_id UUID NOT NULL,
        email_connection_id UUID NOT NULL,
        event_id VARCHAR NOT NULL,
        message_id VARCHAR NOT NULL,
        raw_sha256 VARCHAR NOT NULL,
        notification JSON NOT NULL,
        signed_raw_body VARCHAR NOT NULL,
        signature_headers JSON NOT NULL,
        correlation_id UUID NOT NULL,
        state VARCHAR NOT NULL,
        job_id INTEGER,
        job_version INTEGER NOT NULL,
        lease_token UUID,
        lease_until TIMESTAMP WITH TIME ZONE,
        communication_id UUID,
        agent_run_id UUID,
        error_code VARCHAR,
        review_required BOOLEAN NOT NULL,
        capture_objects JSON NOT NULL,
        id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        version INTEGER NOT NULL,
        PRIMARY KEY (id),
        UNIQUE (tenant_id, id),
        UNIQUE (tenant_id, email_connection_id, event_id),
        FOREIGN KEY(tenant_id, activation_id) REFERENCES email_ingress_activations (tenant_id, id),
        FOREIGN KEY(tenant_id, email_connection_id) REFERENCES provider_connections (tenant_id, id),
        FOREIGN KEY(tenant_id, communication_id) REFERENCES communications (tenant_id, id),
        FOREIGN KEY(tenant_id, agent_run_id) REFERENCES agent_runs (tenant_id, id),
        FOREIGN KEY(tenant_id) REFERENCES tenants (id)
)""",
    """CREATE INDEX ix_email_ingress_receipts_tenant_id ON email_ingress_receipts (tenant_id)""",
    """CREATE TABLE demo_email_inbound_messages (
        tenant_id UUID NOT NULL,
        provider_connection_id UUID NOT NULL,
        native_message_id VARCHAR NOT NULL,
        message JSON NOT NULL,
        payload_sha256 VARCHAR NOT NULL,
        id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        version INTEGER NOT NULL,
        PRIMARY KEY (id),
        UNIQUE (tenant_id, provider_connection_id, native_message_id),
        FOREIGN KEY(tenant_id, provider_connection_id) REFERENCES provider_connections (tenant_id,
id),
        FOREIGN KEY(tenant_id) REFERENCES tenants (id)
)""",
    """CREATE INDEX ix_demo_email_inbound_messages_tenant_id ON demo_email_inbound_messages (tenant_id)""",
)


def upgrade() -> None:
    op.execute(
        "ALTER TABLE communication_threads ADD COLUMN purpose VARCHAR NOT NULL DEFAULT 'invoice_outbound'"
    )
    op.execute("""DO $$ DECLARE constraint_name text; BEGIN
      SELECT conname INTO constraint_name FROM pg_constraint
      WHERE conrelid='communication_threads'::regclass
      AND pg_get_constraintdef(oid)='UNIQUE (tenant_id, provider_connection_id, origin_invoice_id)';
      IF constraint_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE communication_threads DROP CONSTRAINT %I', constraint_name);
      END IF; END $$""")
    op.execute(
        "CREATE UNIQUE INDEX uq_invoice_outbound_thread ON communication_threads "
        "(tenant_id,provider_connection_id,origin_invoice_id) WHERE purpose='invoice_outbound'"
    )
    op.execute(
        "ALTER TABLE communication_threads ALTER COLUMN origin_invoice_id DROP NOT NULL, ALTER "
        "COLUMN customer_id DROP NOT NULL"
    )
    op.execute("ALTER TABLE communications ALTER COLUMN origin_invoice_id DROP NOT NULL")
    op.execute(
        "ALTER TABLE communications ADD COLUMN message_sha256 VARCHAR NULL, ADD COLUMN headers "
        "JSON NOT NULL DEFAULT '[]'"
    )
    for statement in DDL:
        op.execute(statement)


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM email_ingress_activations)
      OR EXISTS (SELECT 1 FROM demo_email_inbound_messages)
      OR EXISTS (SELECT 1 FROM communication_threads WHERE purpose <> 'invoice_outbound')
      THEN RAISE EXCEPTION 'inbound evidence prevents lossy downgrade'; END IF; END $$""")
    for name in ("email_ingress_receipts", "email_ingress_activations", "demo_email_inbound_messages"):
        op.execute("DROP TABLE " + name)
    op.execute("ALTER TABLE communications DROP COLUMN message_sha256, DROP COLUMN headers")
    op.execute("DROP INDEX uq_invoice_outbound_thread")
    op.execute(
        "ALTER TABLE communication_threads DROP COLUMN purpose, ALTER COLUMN origin_invoice_id "
        "SET NOT NULL, ALTER COLUMN customer_id SET NOT NULL, ADD UNIQUE "
        "(tenant_id,provider_connection_id,origin_invoice_id)"
    )
    op.execute("ALTER TABLE communications ALTER COLUMN origin_invoice_id SET NOT NULL")
