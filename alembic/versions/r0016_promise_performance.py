"""Durable non-reused source cash claims and immutable promise verification history."""

import sqlalchemy as sa

from alembic import op

revision = "r0016_promise_performance"
down_revision = "r0015_agent_ingress"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
ALTER TABLE promise_invoice_links ADD COLUMN verification_hold_id UUID REFERENCES
collection_holds(id)
""")
    op.execute("""
CREATE TABLE promise_allocation_claims (
tenant_id UUID NOT NULL,
provider_connection_id UUID NOT NULL,
allocation_id UUID NOT NULL,
claimed_minor BIGINT NOT NULL CHECK (claimed_minor > 0),
promise_id UUID NOT NULL,
installment_id UUID NOT NULL,
id UUID NOT NULL,
created_at TIMESTAMP WITH TIME ZONE NOT NULL,
updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
version INTEGER NOT NULL,
PRIMARY KEY (id),
UNIQUE (tenant_id, allocation_id, installment_id),
FOREIGN KEY(tenant_id, promise_id) REFERENCES payment_promises (tenant_id, id),
FOREIGN KEY(tenant_id, installment_id) REFERENCES promise_installments (tenant_id, id),
FOREIGN KEY(tenant_id, provider_connection_id, allocation_id) REFERENCES
verified_payment_allocations (tenant_id, provider_connection_id, id),
FOREIGN KEY(tenant_id) REFERENCES tenants (id)
)
""")
    op.execute("""
CREATE INDEX ix_promise_allocation_claims_tenant_id ON promise_allocation_claims (tenant_id)
""")
    op.execute("""
CREATE TABLE promise_verifications (
tenant_id UUID NOT NULL,
promise_id UUID NOT NULL,
installment_id UUID NOT NULL,
event_id UUID NOT NULL,
evidence_key VARCHAR NOT NULL,
verification_status VARCHAR NOT NULL,
prior_status VARCHAR NOT NULL,
resulting_status VARCHAR NOT NULL,
timely_minor BIGINT,
late_minor BIGINT,
outside_minor BIGINT,
checked_at TIMESTAMP WITH TIME ZONE NOT NULL,
details JSON NOT NULL,
id UUID NOT NULL,
created_at TIMESTAMP WITH TIME ZONE NOT NULL,
updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
version INTEGER NOT NULL,
PRIMARY KEY (id),
UNIQUE (tenant_id, installment_id, evidence_key),
FOREIGN KEY(tenant_id, promise_id) REFERENCES payment_promises (tenant_id, id),
FOREIGN KEY(tenant_id, installment_id) REFERENCES promise_installments (tenant_id, id),
FOREIGN KEY(tenant_id, event_id) REFERENCES promise_events (tenant_id, id),
CHECK (verification_status IN ('VERIFIED','PENDING_REVIEW')),
FOREIGN KEY(tenant_id) REFERENCES tenants (id)
)
""")
    op.execute("""
CREATE INDEX ix_promise_verifications_tenant_id ON promise_verifications (tenant_id)
""")
    op.execute("""
CREATE FUNCTION bound_promise_cash_claim() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE cash verified_payment_allocations%ROWTYPE; owned UUID; allocated BIGINT;
BEGIN
    SELECT * INTO cash FROM verified_payment_allocations
        WHERE tenant_id=NEW.tenant_id AND id=NEW.allocation_id FOR UPDATE;
    IF NOT FOUND OR cash.kind <> 'cash' OR cash.provider_connection_id <> NEW.provider_connection_id THEN
        RAISE EXCEPTION 'invalid promise source cash';
    END IF;
    IF NOT EXISTS(SELECT 1 FROM promise_installments i JOIN payment_promises p ON p.id=i.promise_id
        WHERE i.id=NEW.installment_id AND i.tenant_id=NEW.tenant_id AND p.id=NEW.promise_id
        AND p.tenant_id=NEW.tenant_id AND p.provider_connection_id=cash.provider_connection_id
        AND EXISTS(SELECT 1 FROM json_array_elements(i.allocations) part
            WHERE part->>'invoice_id'=cash.invoice_id::text
            AND part->'amount'->>'currency'=cash.currency
            AND (part->'amount'->>'exponent')::integer=cash.exponent)) THEN
        RAISE EXCEPTION 'invalid promise installment cash scope';
    END IF;
    SELECT promise_id INTO owned FROM promise_allocation_claims
        WHERE tenant_id=NEW.tenant_id AND allocation_id=NEW.allocation_id LIMIT 1;
    IF owned IS NOT NULL AND owned <> NEW.promise_id THEN
        RAISE EXCEPTION 'source cash already belongs to another promise';
    END IF;
    SELECT coalesce(sum(claimed_minor),0) INTO allocated FROM promise_allocation_claims
        WHERE tenant_id=NEW.tenant_id AND allocation_id=NEW.allocation_id;
    IF allocated + NEW.claimed_minor > cash.minor_units THEN
        RAISE EXCEPTION 'promise source cash overclaimed';
    END IF;
    RETURN NEW;
END $$
""")
    op.execute("""
CREATE TRIGGER promise_cash_claim_bound BEFORE INSERT ON promise_allocation_claims
FOR EACH ROW EXECUTE FUNCTION bound_promise_cash_claim()
""")
    op.execute("""
CREATE TRIGGER promise_allocation_claims_immutable BEFORE UPDATE OR DELETE ON
promise_allocation_claims FOR EACH ROW EXECUTE FUNCTION aco_case_append_only()
""")
    op.execute("""
CREATE TRIGGER promise_verifications_immutable BEFORE UPDATE OR DELETE ON promise_verifications FOR
EACH ROW EXECUTE FUNCTION aco_case_append_only()
""")

    op.add_column("scheduled_actions", sa.Column("promise_event_id", sa.Uuid(), nullable=True))
    op.add_column("scheduled_actions", sa.Column("promise_installment_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_action_promise_event",
        "scheduled_actions",
        "promise_events",
        ["tenant_id", "promise_event_id"],
        ["tenant_id", "id"],
    )
    op.create_foreign_key(
        "fk_action_promise_installment",
        "scheduled_actions",
        "promise_installments",
        ["tenant_id", "promise_installment_id"],
        ["tenant_id", "id"],
    )
    op.drop_constraint("ck_action_trigger", "scheduled_actions", type_="check")
    op.create_check_constraint(
        "ck_action_trigger",
        "scheduled_actions",
        """
(((kind='policy_due' AND agent_run_id IS NULL AND agent_step_id IS NULL AND source_event_id IS NULL
AND policy_event_id IS NOT NULL AND policy_activation_id IS NOT NULL AND occurrence_id IS NOT NULL)
OR (kind IN ('accounting_recheck','internal_task','approval_request') AND source_event_id IS NOT
NULL AND agent_run_id IS NULL AND agent_step_id IS NULL AND policy_event_id IS NULL AND
policy_activation_id IS NULL AND occurrence_id IS NULL) OR (kind='agent_internal' AND
source_event_id IS NULL AND policy_event_id IS NULL AND policy_activation_id IS NULL AND
occurrence_id IS NULL AND policy_version_id IS NULL AND agent_run_id IS NOT NULL AND agent_step_id
IS NOT NULL)) AND promise_event_id IS NULL AND promise_installment_id IS NULL) OR
(kind='promise_check' AND promise_event_id IS NOT NULL AND promise_installment_id IS NOT NULL AND
source_event_id IS NULL AND policy_event_id IS NULL AND policy_activation_id IS NULL AND
occurrence_id IS NULL AND policy_version_id IS NULL AND agent_run_id IS NULL AND agent_step_id IS
NULL AND reservation_id IS NULL)
""",
    )


def downgrade() -> None:
    raise RuntimeError("Promise performance evidence cannot be discarded by downgrade")
