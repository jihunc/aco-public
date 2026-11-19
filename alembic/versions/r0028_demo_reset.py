"""Immutable generation-fenced synthetic reset history; preserve all old effects."""

from alembic import op

revision = "r0028_demo_reset"
down_revision = "r0027_voice_consent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE demo_resets (
      id uuid PRIMARY KEY, created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL,
      version integer NOT NULL, tenant_id uuid NOT NULL REFERENCES tenants(id),
      actor_id uuid NOT NULL, request_id uuid NOT NULL, audit_id uuid NOT NULL UNIQUE,
      previous_generation integer NOT NULL, generation integer NOT NULL,
      command json NOT NULL, result json NOT NULL,
      UNIQUE(tenant_id,id), UNIQUE(tenant_id,request_id),
      FOREIGN KEY(tenant_id,actor_id) REFERENCES memberships(tenant_id,user_id),
      FOREIGN KEY(tenant_id,audit_id) REFERENCES identity_audit_events(tenant_id,id),
      CHECK(previous_generation >= 1 AND generation = previous_generation + 1),
      CHECK(json_typeof(command) = 'object' AND json_typeof(result) = 'object'))""")
    op.execute("CREATE INDEX ix_demo_resets_tenant_id ON demo_resets(tenant_id)")
    op.execute("""CREATE FUNCTION protect_demo_reset() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'demo reset history is immutable'; END $$""")
    op.execute("""CREATE TRIGGER demo_reset_immutable BEFORE UPDATE OR DELETE ON demo_resets
      FOR EACH ROW EXECUTE FUNCTION protect_demo_reset()""")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM demo_resets) THEN
      RAISE EXCEPTION 'retained reset history prevents lossy downgrade'; END IF; END $$""")
    op.drop_table("demo_resets")
    op.execute("DROP FUNCTION protect_demo_reset()")
