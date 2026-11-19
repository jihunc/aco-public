"""Bind provider ingress to immutable server-approved capture material."""

from alembic import op

revision = "r0019_provider_ingress"
down_revision = "r0018_impact_cohorts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE email_ingress_activations ADD COLUMN provider_binding JSON")
    op.execute("""CREATE FUNCTION protect_provider_ingress_binding() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN
        IF OLD.provider_binding IS NOT NULL THEN
          RAISE EXCEPTION 'provider ingress binding is immutable';
        END IF;
        RETURN OLD;
      END IF;
      IF OLD.provider_binding IS NOT NULL OR NEW.provider_binding IS NOT NULL THEN
        IF (to_jsonb(NEW) - ARRAY['revoked_at','version','updated_at']) IS DISTINCT FROM
           (to_jsonb(OLD) - ARRAY['revoked_at','version','updated_at']) THEN
          RAISE EXCEPTION 'provider ingress binding is immutable';
        END IF;
        IF OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at THEN
          RAISE EXCEPTION 'provider ingress revocation is irreversible';
        END IF;
      END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER provider_ingress_binding_immutable BEFORE UPDATE OR DELETE "
        "ON email_ingress_activations FOR EACH ROW EXECUTE FUNCTION protect_provider_ingress_binding()"
    )


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS (
      SELECT 1 FROM email_ingress_activations WHERE provider_binding IS NOT NULL)
      THEN RAISE EXCEPTION 'provider ingress history prevents lossy downgrade'; END IF; END $$""")
    op.execute("DROP TRIGGER provider_ingress_binding_immutable ON email_ingress_activations")
    op.execute("DROP FUNCTION protect_provider_ingress_binding()")
    op.drop_column("email_ingress_activations", "provider_binding")
