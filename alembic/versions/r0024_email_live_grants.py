"""Explicit operator email test grants and immutable approved retention; no live grant seed."""

from alembic import op

revision = "r0024_email_live_grants"
down_revision = "r0023_t25_economics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE artifact_retention_policies ADD COLUMN approval JSON")
    op.execute("""DO $$ DECLARE item record; BEGIN
      FOR item IN SELECT conname FROM pg_constraint WHERE
        conrelid='artifact_retention_policies'::regclass AND contype='c'
        AND pg_get_constraintdef(oid) LIKE '%retention_days%'
      LOOP EXECUTE format('ALTER TABLE artifact_retention_policies DROP CONSTRAINT %I',item.conname);
      END LOOP;
    END $$""")
    op.execute("""ALTER TABLE artifact_retention_policies ADD CONSTRAINT approved_retention_scope CHECK (
      (synthetic AND retention_days=30 AND approval IS NULL) OR
      (NOT synthetic AND retention_days BETWEEN 1 AND 30 AND approval IS NOT NULL))""")
    op.execute("""CREATE TABLE email_live_grants (
      id uuid PRIMARY KEY,created_at timestamptz NOT NULL,updated_at timestamptz NOT NULL,
      version integer NOT NULL,
      tenant_id uuid NOT NULL REFERENCES tenants(id),tenant_generation integer NOT NULL,
      initiator_id uuid NOT NULL REFERENCES identity_actors(id),membership_version integer NOT NULL,
      connection_id uuid NOT NULL,connection_version integer NOT NULL,storage_connection_id uuid NOT NULL,
      storage_connection_version integer NOT NULL,account_id text NOT NULL,credential_revision text NOT NULL,
      authorization_ref text NOT NULL UNIQUE,material json NOT NULL,retention_policy_id uuid NOT NULL,
      retention_policy_version integer NOT NULL,scope_ids json NOT NULL,revoked_at timestamptz,
      UNIQUE(tenant_id,id),
      FOREIGN KEY(tenant_id,connection_id) REFERENCES provider_connections(tenant_id,id),
      FOREIGN KEY(tenant_id,storage_connection_id) REFERENCES provider_connections(tenant_id,id),
      FOREIGN KEY(tenant_id,retention_policy_id) REFERENCES artifact_retention_policies(tenant_id,id))""")
    op.execute("""CREATE FUNCTION protect_email_live_grant() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'email live grant history is immutable'; END IF;
      IF (to_jsonb(NEW)-ARRAY['revoked_at','updated_at','version']) IS DISTINCT FROM
         (to_jsonb(OLD)-ARRAY['revoked_at','updated_at','version']) OR
         (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at)
      THEN RAISE EXCEPTION 'email live grant material is immutable'; END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER email_live_grant_immutable BEFORE UPDATE OR DELETE ON email_live_grants "
        "FOR EACH ROW EXECUTE FUNCTION protect_email_live_grant()"
    )
    op.execute("""CREATE FUNCTION protect_approved_retention() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF OLD.approval IS NOT NULL THEN RAISE EXCEPTION 'approved retention material is immutable'; END IF;
      IF TG_OP='UPDATE' AND NEW.approval IS NOT NULL THEN
        RAISE EXCEPTION 'synthetic policy cannot be promoted'; END IF;
      IF TG_OP='DELETE' THEN RETURN OLD; END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER approved_retention_immutable BEFORE UPDATE OR DELETE ON artifact_retention_policies "
        "FOR EACH ROW EXECUTE FUNCTION protect_approved_retention()"
    )


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM email_live_grants) OR
      EXISTS(SELECT 1 FROM artifact_retention_policies WHERE approval IS NOT NULL)
      THEN RAISE EXCEPTION 'retained live approval history prevents lossy downgrade'; END IF; END $$""")
    op.drop_table("email_live_grants")
    op.execute("DROP FUNCTION protect_email_live_grant()")
    op.execute("DROP TRIGGER approved_retention_immutable ON artifact_retention_policies")
    op.execute("DROP FUNCTION protect_approved_retention()")
    op.execute("ALTER TABLE artifact_retention_policies DROP CONSTRAINT approved_retention_scope")
    op.execute("ALTER TABLE artifact_retention_policies DROP COLUMN approval")
    op.execute("ALTER TABLE artifact_retention_policies ADD CHECK(retention_days=30 AND synthetic)")
