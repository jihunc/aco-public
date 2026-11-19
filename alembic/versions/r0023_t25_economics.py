"""Versioned reviewed assumptions and one canonical allocation basis per usage component."""

from alembic import op

revision = "r0023_t25_economics"
down_revision = "r0022_email_mailboxes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    common = """id UUID PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
      version INTEGER NOT NULL CHECK(version > 0), tenant_id UUID NOT NULL REFERENCES tenants(id),
      request_id UUID NOT NULL, actor_id UUID NOT NULL REFERENCES identity_actors(id),
      audit_id UUID NOT NULL, command JSON NOT NULL, UNIQUE(tenant_id,request_id),
      FOREIGN KEY(tenant_id,audit_id) REFERENCES identity_audit_events(tenant_id,id)"""
    op.execute(f"""CREATE TABLE economics_assumptions ({common},
      previous_version_id UUID, reviewed_hash VARCHAR NOT NULL,
      UNIQUE(tenant_id,id), UNIQUE(tenant_id,version),
      FOREIGN KEY(tenant_id,previous_version_id) REFERENCES economics_assumptions(tenant_id,id))""")
    op.execute(f"""CREATE TABLE economics_approvals ({common}, assumptions_id UUID NOT NULL,
      previous_approval_id UUID, UNIQUE(tenant_id,id), UNIQUE(tenant_id,version),
      UNIQUE(tenant_id,assumptions_id),
      FOREIGN KEY(tenant_id,assumptions_id) REFERENCES economics_assumptions(tenant_id,id),
      FOREIGN KEY(tenant_id,previous_approval_id) REFERENCES economics_approvals(tenant_id,id))""")
    op.execute(f"""CREATE TABLE cost_allocation_versions ({common}, event_id UUID NOT NULL,
      revision_id UUID NOT NULL REFERENCES usage_revisions(id), previous_version_id UUID,
      basis VARCHAR NOT NULL CHECK(basis IN ('EQUAL_VERIFIED_LEAVES','OWNER_REVIEWED_WEIGHTS','UNALLOCATED')),
      leaves JSON NOT NULL, source_ids JSON NOT NULL,
      UNIQUE(tenant_id,event_id,id), UNIQUE(tenant_id,event_id,version),
      FOREIGN KEY(tenant_id,event_id) REFERENCES provider_usage_events(tenant_id,id),
      FOREIGN KEY(tenant_id,event_id,previous_version_id)
        REFERENCES cost_allocation_versions(tenant_id,event_id,id))""")
    op.execute("""CREATE FUNCTION aco_cost_allocation_scope() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE leaf JSON; total NUMERIC := 0; n INTEGER := 0; invoice UUID; issue UUID;
    BEGIN
      IF NOT EXISTS(SELECT 1 FROM usage_revisions WHERE id=NEW.revision_id AND event_id=NEW.event_id) THEN
        RAISE EXCEPTION 'allocation revision must belong to the exact event';
      END IF;
      IF json_typeof(NEW.leaves) <> 'array' OR json_array_length(NEW.leaves) < 1 THEN
        RAISE EXCEPTION 'allocation requires leaves';
      END IF;
      FOR leaf IN SELECT * FROM json_array_elements(NEW.leaves) LOOP
        invoice := (leaf->>'invoice_id')::uuid; issue := (leaf->>'case_id')::uuid;
        IF leaf->>'weight' IS NULL OR (leaf->>'weight')::numeric <= 0
          OR (leaf->>'weight')::numeric > 1 OR scale((leaf->>'weight')::numeric) > 12 THEN
          RAISE EXCEPTION 'invalid allocation weight';
        END IF;
        total := total + (leaf->>'weight')::numeric; n := n + 1;
        IF invoice IS NOT NULL AND NOT EXISTS(SELECT 1 FROM invoice_snapshots
          WHERE id=invoice AND tenant_id=NEW.tenant_id) THEN RAISE EXCEPTION 'foreign invoice'; END IF;
        IF issue IS NOT NULL AND (invoice IS NULL OR NOT EXISTS(SELECT 1 FROM case_invoice_links
          WHERE case_id=issue AND invoice_id=invoice AND tenant_id=NEW.tenant_id)) THEN
          RAISE EXCEPTION 'foreign case/invoice'; END IF;
        IF invoice IS NULL AND json_array_length(NEW.leaves) <> 1 THEN
          RAISE EXCEPTION 'unallocated bucket must be the sole leaf'; END IF;
      END LOOP;
      IF total <> 1 OR n <> (SELECT count(DISTINCT (x->>'case_id',x->>'invoice_id'))
        FROM json_array_elements(NEW.leaves) x) THEN
        RAISE EXCEPTION 'allocation must be unique and sum one'; END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER cost_allocation_scope BEFORE INSERT ON cost_allocation_versions "
        "FOR EACH ROW EXECUTE FUNCTION aco_cost_allocation_scope()"
    )
    for table in ("economics_assumptions", "economics_approvals", "cost_allocation_versions"):
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION aco_case_append_only()"
        )


def downgrade() -> None:
    for table in ("cost_allocation_versions", "economics_approvals", "economics_assumptions"):
        op.execute(
            f"DO $$ BEGIN IF EXISTS(SELECT 1 FROM {table}) THEN "
            "RAISE EXCEPTION 'economics history prevents lossy downgrade'; END IF; END $$"
        )
    op.drop_table("cost_allocation_versions")
    op.drop_table("economics_approvals")
    op.drop_table("economics_assumptions")
    op.execute("DROP FUNCTION aco_cost_allocation_scope()")
