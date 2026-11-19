"""Separate current read evidence from immutable source financial history."""

from alembic import op

revision = "r0002b_accounting_observations"
down_revision = "r0002_financials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE accounting_events ADD COLUMN origin VARCHAR NOT NULL DEFAULT 'SOURCE_HINT'")
    op.execute("ALTER TABLE accounting_events ADD COLUMN invoice_id UUID")
    op.execute("ALTER TABLE accounting_events ADD COLUMN source_revision VARCHAR")
    # Only new relation metadata is populated; immutable original payload/result/provenance stay intact.
    # The trigger is restored in the same transactional migration before runtime can observe the change.
    op.execute("ALTER TABLE accounting_events DISABLE TRIGGER immutable_history")
    op.execute("""UPDATE accounting_events SET invoice_id = (result->>'invoice_id')::uuid,
        source_revision = result->>'source_revision'""")
    op.execute("ALTER TABLE accounting_events ENABLE TRIGGER immutable_history")
    op.execute("ALTER TABLE accounting_events ALTER COLUMN invoice_id SET NOT NULL")
    op.execute("ALTER TABLE accounting_events ALTER COLUMN source_revision SET NOT NULL")
    op.execute("ALTER TABLE accounting_events ALTER COLUMN external_event_id DROP NOT NULL")
    op.execute("""ALTER TABLE accounting_events ADD CONSTRAINT fk_accounting_event_invoice
        FOREIGN KEY (tenant_id, provider_connection_id, invoice_id)
        REFERENCES invoice_snapshots(tenant_id, provider_connection_id, id)""")
    op.execute("""ALTER TABLE accounting_events ADD CONSTRAINT ck_accounting_event_origin CHECK (
        (origin = 'SOURCE_HINT' AND external_event_id IS NOT NULL) OR
        (origin = 'AUTHORITATIVE_READ' AND external_event_id IS NULL))""")
    op.execute("""CREATE UNIQUE INDEX uq_accounting_read_revision ON accounting_events
        (tenant_id, provider_connection_id, invoice_id, source_revision)
        WHERE origin = 'AUTHORITATIVE_READ'""")
    op.execute("ALTER TABLE invoice_snapshots ADD COLUMN last_read_at TIMESTAMP WITH TIME ZONE")
    op.execute("""CREATE TABLE accounting_observations (
        invoice_id UUID NOT NULL,
        actor_id UUID NOT NULL REFERENCES identity_users(id),
        external_event_id VARCHAR,
        invoice_data JSON NOT NULL,
        payment_data JSON NOT NULL,
        provenance JSON NOT NULL,
        observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
        tenant_id UUID NOT NULL REFERENCES tenants(id),
        provider_connection_id UUID NOT NULL,
        id UUID NOT NULL PRIMARY KEY,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        version INTEGER NOT NULL,
        FOREIGN KEY (tenant_id, provider_connection_id)
            REFERENCES provider_connections(tenant_id, id),
        FOREIGN KEY (tenant_id, provider_connection_id, invoice_id)
            REFERENCES invoice_snapshots(tenant_id, provider_connection_id, id),
        UNIQUE (tenant_id, provider_connection_id, id)
    )""")
    op.execute("CREATE INDEX ix_accounting_observations_tenant_id ON accounting_observations (tenant_id)")
    op.execute(
        "CREATE INDEX ix_accounting_observations_provider_connection_id "
        "ON accounting_observations (provider_connection_id)"
    )
    op.execute("""CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON accounting_observations
        FOR EACH ROW EXECUTE FUNCTION reject_financial_history_mutation()""")


def downgrade() -> None:
    # Old schema cannot represent a change without a native hint identity. Never invent one or erase evidence.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM accounting_events WHERE origin = 'AUTHORITATIVE_READ') THEN
            RAISE EXCEPTION 'cannot downgrade: authoritative-read history requires current schema';
        END IF;
    END $$""")
    op.drop_index("uq_accounting_read_revision", table_name="accounting_events")
    op.drop_constraint("ck_accounting_event_origin", "accounting_events", type_="check")
    op.drop_constraint("fk_accounting_event_invoice", "accounting_events", type_="foreignkey")
    op.execute("ALTER TABLE accounting_events ALTER COLUMN external_event_id SET NOT NULL")
    op.drop_column("accounting_events", "source_revision")
    op.drop_column("accounting_events", "invoice_id")
    op.drop_column("accounting_events", "origin")
    op.drop_table("accounting_observations")
    op.drop_column("invoice_snapshots", "last_read_at")
