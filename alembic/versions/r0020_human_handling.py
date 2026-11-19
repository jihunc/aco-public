"""Retain explicit human-reported time and every correction against actual case events."""

from alembic import op

revision = "r0020_human_handling"
down_revision = "r0019_provider_ingress"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE human_handling_entries (
      id UUID PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
      version INTEGER NOT NULL CHECK(version > 0), tenant_id UUID NOT NULL REFERENCES tenants(id),
      case_id UUID NOT NULL, history_id UUID NOT NULL REFERENCES case_history(id),
      actor_id UUID NOT NULL REFERENCES identity_actors(id), audit_id UUID NOT NULL,
      request_id UUID NOT NULL, command JSON NOT NULL, minutes NUMERIC(8,2) NOT NULL,
      evidence VARCHAR NOT NULL, supersedes_id UUID,
      CHECK(minutes >= 0 AND minutes <= 1440), UNIQUE(tenant_id,request_id),
      UNIQUE(tenant_id,history_id,id), UNIQUE(tenant_id,history_id,version),
      FOREIGN KEY(tenant_id,case_id) REFERENCES collection_cases(tenant_id,id),
      FOREIGN KEY(tenant_id,audit_id) REFERENCES identity_audit_events(tenant_id,id),
      FOREIGN KEY(tenant_id,history_id,supersedes_id)
        REFERENCES human_handling_entries(tenant_id,history_id,id))""")
    op.create_index("ix_human_handling_entries_tenant_id", "human_handling_entries", ["tenant_id"])
    op.execute("""CREATE FUNCTION aco_handling_scope() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NOT EXISTS(SELECT 1 FROM case_history h WHERE h.id=NEW.history_id
        AND h.tenant_id=NEW.tenant_id AND h.case_id=NEW.case_id
        AND h.actor_id=NEW.actor_id AND h.substantive_human) THEN
        RAISE EXCEPTION 'handling requires own scoped substantive history';
      END IF;
      IF NEW.supersedes_id IS NULL AND NEW.version <> 1 THEN
        RAISE EXCEPTION 'initial handling entry version must be one';
      ELSIF NEW.supersedes_id IS NOT NULL AND NOT EXISTS(
        SELECT 1 FROM human_handling_entries e WHERE e.id=NEW.supersedes_id
          AND e.tenant_id=NEW.tenant_id AND e.history_id=NEW.history_id
          AND e.actor_id=NEW.actor_id AND e.version=NEW.version-1) THEN
        RAISE EXCEPTION 'handling correction must preserve its original actor and event';
      END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER handling_scope BEFORE INSERT ON human_handling_entries "
        "FOR EACH ROW EXECUTE FUNCTION aco_handling_scope()"
    )
    op.execute(
        "CREATE TRIGGER handling_immutable BEFORE UPDATE OR DELETE ON human_handling_entries "
        "FOR EACH ROW EXECUTE FUNCTION aco_case_append_only()"
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS(SELECT 1 FROM human_handling_entries) THEN "
        "RAISE EXCEPTION 'handling history prevents lossy downgrade'; END IF; END $$"
    )
    op.drop_table("human_handling_entries")
    op.execute("DROP FUNCTION aco_handling_scope()")
