"""Frozen additive evidence-preserving domain migration."""

import sqlalchemy as sa

from alembic import op

revision = "r0009_promises"
down_revision = "r0008_cases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE payment_promises (
    tenant_id UUID NOT NULL, 
    customer_id UUID NOT NULL, 
    provider_connection_id UUID NOT NULL, 
    capture_key VARCHAR NOT NULL, 
    status VARCHAR NOT NULL, 
    source_communication_id UUID NOT NULL, 
    source_text VARCHAR NOT NULL, 
    source_offset INTEGER NOT NULL, 
    source_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    source_state VARCHAR NOT NULL, 
    synthetic BOOLEAN NOT NULL, 
    amount JSON, 
    due_date DATE, 
    timezone VARCHAR, 
    cutoff_at TIMESTAMP WITH TIME ZONE, 
    confirmed_at TIMESTAMP WITH TIME ZONE, 
    confirmation_communication_id UUID, 
    confirmation_reasons JSON NOT NULL, 
    candidate_invoice_ids JSON NOT NULL, 
    replaces_promise_id UUID, 
    superseded_by_id UUID, 
    review_task_id UUID, 
    command JSON NOT NULL, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    version INTEGER NOT NULL, 
    PRIMARY KEY (id), 
    UNIQUE (tenant_id, id), 
    UNIQUE (tenant_id, capture_key), 
    FOREIGN KEY(tenant_id, provider_connection_id, customer_id) REFERENCES customers
        (tenant_id, provider_connection_id, id), 
    FOREIGN KEY(tenant_id, source_communication_id) REFERENCES communications (tenant_id, id), 
    FOREIGN KEY(tenant_id, confirmation_communication_id) REFERENCES communications (tenant_id, id), 
    FOREIGN KEY(tenant_id, replaces_promise_id) REFERENCES payment_promises (tenant_id, id), 
    FOREIGN KEY(tenant_id, superseded_by_id) REFERENCES payment_promises (tenant_id, id), 
    CHECK (status IN ('CAPTURED','NEEDS_CONFIRMATION','ACTIVE','FULFILLED','MISSED',
        'SUPERSEDED','CANCELLED')), 
    FOREIGN KEY(tenant_id) REFERENCES tenants (id), 
    FOREIGN KEY(review_task_id) REFERENCES collection_tasks (id)
)

""")
    op.execute("""CREATE INDEX ix_payment_promises_tenant_id ON payment_promises (tenant_id)""")
    op.execute("""
CREATE TABLE promise_invoice_links (
    tenant_id UUID NOT NULL, 
    promise_id UUID NOT NULL, 
    invoice_id UUID NOT NULL, 
    amount JSON, 
    hold_id UUID, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    version INTEGER NOT NULL, 
    PRIMARY KEY (id), 
    UNIQUE (tenant_id, promise_id, invoice_id), 
    FOREIGN KEY(tenant_id, promise_id) REFERENCES payment_promises (tenant_id, id), 
    FOREIGN KEY(tenant_id, invoice_id) REFERENCES invoice_snapshots (tenant_id, id), 
    FOREIGN KEY(tenant_id) REFERENCES tenants (id), 
    FOREIGN KEY(hold_id) REFERENCES collection_holds (id)
)

""")
    op.execute("""CREATE INDEX ix_promise_invoice_links_tenant_id ON promise_invoice_links (tenant_id)""")
    op.execute("""
CREATE TABLE promise_installments (
    tenant_id UUID NOT NULL, 
    promise_id UUID NOT NULL, 
    sequence INTEGER NOT NULL, 
    due_date DATE NOT NULL, 
    cutoff_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    amount JSON NOT NULL, 
    allocations JSON NOT NULL, 
    status VARCHAR NOT NULL, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    version INTEGER NOT NULL, 
    PRIMARY KEY (id), 
    UNIQUE (tenant_id, id), 
    UNIQUE (tenant_id, promise_id, sequence), 
    FOREIGN KEY(tenant_id, promise_id) REFERENCES payment_promises (tenant_id, id), 
    FOREIGN KEY(tenant_id) REFERENCES tenants (id)
)

""")
    op.execute("""CREATE INDEX ix_promise_installments_tenant_id ON promise_installments (tenant_id)""")
    op.execute("""
CREATE TABLE promise_events (
    tenant_id UUID NOT NULL, 
    promise_id UUID NOT NULL, 
    actor_id UUID NOT NULL, 
    audit_event_id UUID NOT NULL, 
    event VARCHAR NOT NULL, 
    details JSON NOT NULL, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    version INTEGER NOT NULL, 
    PRIMARY KEY (id), 
    UNIQUE (tenant_id, id), 
    FOREIGN KEY(tenant_id, promise_id) REFERENCES payment_promises (tenant_id, id), 
    FOREIGN KEY(tenant_id) REFERENCES tenants (id), 
    FOREIGN KEY(actor_id) REFERENCES identity_actors (id), 
    FOREIGN KEY(audit_event_id) REFERENCES identity_audit_events (id)
)

""")
    op.execute("""CREATE INDEX ix_promise_events_tenant_id ON promise_events (tenant_id)""")
    op.execute("""
CREATE TABLE customer_scope_reviews (
    tenant_id UUID NOT NULL, 
    provider_connection_id UUID NOT NULL, 
    customer_id UUID NOT NULL, 
    promise_id UUID NOT NULL, 
    reason VARCHAR NOT NULL, 
    hold_ids JSON NOT NULL, 
    expires_at TIMESTAMP WITH TIME ZONE, 
    released_at TIMESTAMP WITH TIME ZONE, 
    release_evidence VARCHAR, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    version INTEGER NOT NULL, 
    PRIMARY KEY (id), 
    UNIQUE (tenant_id, promise_id), 
    FOREIGN KEY(tenant_id, provider_connection_id, customer_id) REFERENCES customers
        (tenant_id, provider_connection_id, id), 
    FOREIGN KEY(tenant_id, promise_id) REFERENCES payment_promises (tenant_id, id), 
    FOREIGN KEY(tenant_id) REFERENCES tenants (id)
)

""")
    op.execute("""CREATE INDEX ix_customer_scope_reviews_tenant_id ON customer_scope_reviews (tenant_id)""")
    op.execute(
        """CREATE INDEX ix_customer_scope_reviews_customer_id ON customer_scope_reviews (customer_id)"""
    )
    op.execute("""CREATE TRIGGER promise_events_immutable
    BEFORE UPDATE OR DELETE ON promise_events
    FOR EACH ROW EXECUTE FUNCTION aco_case_append_only()""")


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT 1 FROM payment_promises)")):
        raise RuntimeError("cannot downgrade: retained domain evidence would be lost")
    op.drop_table("customer_scope_reviews")
    op.drop_table("promise_events")
    op.drop_table("promise_installments")
    op.drop_table("promise_invoice_links")
    op.drop_table("payment_promises")
