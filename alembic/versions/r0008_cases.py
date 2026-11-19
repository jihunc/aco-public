"""Frozen additive evidence-preserving domain migration."""

import sqlalchemy as sa

from alembic import op

revision = "r0008_cases"
down_revision = "r0011_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE collection_cases (
    tenant_id UUID NOT NULL, 
    customer_id UUID NOT NULL, 
    provider_connection_id UUID NOT NULL, 
    issue_identity VARCHAR NOT NULL, 
    issue_type VARCHAR NOT NULL, 
    claim_key VARCHAR NOT NULL, 
    substance VARCHAR NOT NULL, 
    status VARCHAR NOT NULL, 
    review_required BOOLEAN NOT NULL, 
    task_id UUID NOT NULL, 
    synthetic BOOLEAN NOT NULL, 
    resolved_at TIMESTAMP WITH TIME ZONE, 
    closed_at TIMESTAMP WITH TIME ZONE, 
    closure_reason VARCHAR, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    version INTEGER NOT NULL, 
    PRIMARY KEY (id), 
    UNIQUE (tenant_id, id), 
    UNIQUE (tenant_id, issue_identity), 
    FOREIGN KEY(tenant_id, provider_connection_id, customer_id) REFERENCES customers
        (tenant_id, provider_connection_id, id), 
    CHECK (status IN ('OPEN','INVESTIGATING','WAITING_CUSTOMER','WAITING_INTERNAL',
        'AWAITING_APPROVAL','RESOLVED','CLOSED')), 
    FOREIGN KEY(tenant_id) REFERENCES tenants (id), 
    FOREIGN KEY(task_id) REFERENCES collection_tasks (id)
)

""")
    op.execute("""CREATE INDEX ix_collection_cases_tenant_id ON collection_cases (tenant_id)""")
    op.execute("""
CREATE TABLE case_evidence (
    tenant_id UUID NOT NULL, 
    case_id UUID NOT NULL, 
    communication_id UUID NOT NULL, 
    kind VARCHAR NOT NULL, 
    evidence_key VARCHAR NOT NULL, 
    excerpt VARCHAR NOT NULL, 
    "offset" INTEGER NOT NULL, 
    synthetic BOOLEAN NOT NULL, 
    source_state VARCHAR NOT NULL, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    version INTEGER NOT NULL, 
    PRIMARY KEY (id), 
    UNIQUE (tenant_id, id), 
    UNIQUE (tenant_id, case_id, evidence_key), 
    FOREIGN KEY(tenant_id, case_id) REFERENCES collection_cases (tenant_id, id), 
    FOREIGN KEY(tenant_id, communication_id) REFERENCES communications (tenant_id, id), 
    FOREIGN KEY(tenant_id) REFERENCES tenants (id)
)

""")
    op.execute("""CREATE INDEX ix_case_evidence_tenant_id ON case_evidence (tenant_id)""")
    op.execute("""
CREATE TABLE case_invoice_links (
    tenant_id UUID NOT NULL, 
    case_id UUID NOT NULL, 
    invoice_id UUID NOT NULL, 
    issue_state VARCHAR NOT NULL, 
    impact VARCHAR NOT NULL, 
    hold_id UUID, 
    resolution_action VARCHAR, 
    resolution_evidence_id UUID, 
    resolved_at TIMESTAMP WITH TIME ZONE, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    version INTEGER NOT NULL, 
    PRIMARY KEY (id), 
    UNIQUE (tenant_id, case_id, invoice_id), 
    FOREIGN KEY(tenant_id, case_id) REFERENCES collection_cases (tenant_id, id), 
    FOREIGN KEY(tenant_id, invoice_id) REFERENCES invoice_snapshots (tenant_id, id), 
    FOREIGN KEY(tenant_id, resolution_evidence_id) REFERENCES case_evidence (tenant_id, id), 
    FOREIGN KEY(tenant_id) REFERENCES tenants (id), 
    FOREIGN KEY(hold_id) REFERENCES collection_holds (id)
)

""")
    op.execute("""CREATE INDEX ix_case_invoice_links_tenant_id ON case_invoice_links (tenant_id)""")
    op.execute("""
CREATE TABLE case_history (
    tenant_id UUID NOT NULL, 
    case_id UUID NOT NULL, 
    actor_id UUID NOT NULL, 
    event VARCHAR NOT NULL, 
    substantive_human BOOLEAN NOT NULL, 
    details JSON NOT NULL, 
    id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    version INTEGER NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(tenant_id, case_id) REFERENCES collection_cases (tenant_id, id), 
    FOREIGN KEY(tenant_id) REFERENCES tenants (id), 
    FOREIGN KEY(actor_id) REFERENCES identity_actors (id)
)

""")
    op.execute("""CREATE INDEX ix_case_history_tenant_id ON case_history (tenant_id)""")
    op.execute("""CREATE FUNCTION aco_case_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'domain history is append-only'; END $$""")
    op.execute("""CREATE TRIGGER case_evidence_immutable
    BEFORE UPDATE OR DELETE ON case_evidence
    FOR EACH ROW EXECUTE FUNCTION aco_case_append_only()""")
    op.execute("""CREATE TRIGGER case_history_immutable
    BEFORE UPDATE OR DELETE ON case_history
    FOR EACH ROW EXECUTE FUNCTION aco_case_append_only()""")


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT 1 FROM collection_cases)")):
        raise RuntimeError("cannot downgrade: retained domain evidence would be lost")
    op.drop_table("case_history")
    op.drop_table("case_invoice_links")
    op.drop_table("case_evidence")
    op.drop_table("collection_cases")
    op.execute("DROP FUNCTION aco_case_append_only()")
