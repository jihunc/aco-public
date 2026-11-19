"""Global mailbox ownership and immutable shared-account webhook admission."""

from alembic import op

revision = "r0022_email_mailboxes"
down_revision = "r0021_voice"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE email_execution_activations ADD COLUMN provider_binding JSON")
    common = (
        "id uuid PRIMARY KEY,created_at timestamptz NOT NULL,"
        "updated_at timestamptz NOT NULL,version integer NOT NULL"
    )
    op.execute(f"""CREATE TABLE email_mailbox_claims ({common},
      tenant_id uuid NOT NULL REFERENCES tenants(id),activation_id uuid NOT NULL,
      email_connection_id uuid NOT NULL,address text NOT NULL,released_at timestamptz,
      UNIQUE(tenant_id,id),
      FOREIGN KEY(tenant_id,activation_id) REFERENCES email_ingress_activations(tenant_id,id),
      FOREIGN KEY(tenant_id,email_connection_id) REFERENCES provider_connections(tenant_id,id),
      CHECK(address=lower(address) AND length(address) BETWEEN 3 AND 320))""")
    op.execute(
        "CREATE UNIQUE INDEX email_mailbox_one_active_owner ON email_mailbox_claims(address) "
        "WHERE released_at IS NULL"
    )
    op.execute(f"""CREATE TABLE email_mailbox_conflicts ({common},address text NOT NULL UNIQUE,
      activation_ids json NOT NULL,resolved_at timestamptz)""")
    op.execute(f"""CREATE TABLE email_webhook_admissions ({common},
      tenant_id uuid NOT NULL REFERENCES tenants(id),endpoint_id uuid NOT NULL,
      account_id text NOT NULL,event_id text NOT NULL,recipients json NOT NULL,raw_sha256 text NOT NULL,
      activation_id uuid NOT NULL,email_connection_id uuid NOT NULL,receipt_id uuid NOT NULL,
      UNIQUE(endpoint_id,event_id),UNIQUE(account_id,event_id),
      FOREIGN KEY(tenant_id,activation_id) REFERENCES email_ingress_activations(tenant_id,id),
      FOREIGN KEY(tenant_id,email_connection_id) REFERENCES provider_connections(tenant_id,id),
      FOREIGN KEY(tenant_id,receipt_id) REFERENCES email_ingress_receipts(tenant_id,id))""")
    # Preserve every prior row/column. Conflicting legacy owners get no active claim.
    eligible = """SELECT DISTINCT lower(trim(address)) AS address,a.id,a.tenant_id,
      a.email_connection_id,a.created_at FROM email_ingress_activations a,
      LATERAL json_array_elements_text(a.provider_binding->'inbox_addresses') address
      WHERE a.provider_binding IS NOT NULL AND a.revoked_at IS NULL AND a.expires_at>now()"""
    op.execute(f"""WITH eligible AS ({eligible}), conflicts AS (
      SELECT address,json_agg(id::text) ids FROM eligible GROUP BY address
      HAVING count(DISTINCT (tenant_id,email_connection_id))>1)
      INSERT INTO email_mailbox_conflicts
      SELECT gen_random_uuid(),now(),now(),1,address,ids,NULL FROM conflicts""")
    op.execute(f"""WITH eligible AS ({eligible}), ranked AS (
      SELECT *,row_number() OVER(PARTITION BY address ORDER BY created_at DESC,id DESC) position
      FROM eligible WHERE address NOT IN (SELECT address FROM email_mailbox_conflicts))
      INSERT INTO email_mailbox_claims
      SELECT gen_random_uuid(),now(),now(),1,tenant_id,id,email_connection_id,address,NULL
      FROM ranked WHERE position=1""")
    op.execute("""CREATE FUNCTION protect_email_mailbox_history() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'mailbox history is immutable'; END IF;
      IF (to_jsonb(NEW)-ARRAY['released_at','resolved_at','updated_at','version']) IS DISTINCT FROM
         (to_jsonb(OLD)-ARRAY['released_at','resolved_at','updated_at','version']) THEN
        RAISE EXCEPTION 'mailbox ownership is immutable'; END IF;
      IF TG_TABLE_NAME='email_mailbox_claims' AND to_jsonb(OLD)->>'released_at' IS NOT NULL
         AND to_jsonb(NEW)->>'released_at' IS DISTINCT FROM to_jsonb(OLD)->>'released_at' THEN
        RAISE EXCEPTION 'mailbox release is irreversible'; END IF;
      RETURN NEW;
    END $$""")
    for table in ("email_mailbox_claims", "email_mailbox_conflicts"):
        op.execute(
            f"CREATE TRIGGER mailbox_history_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION protect_email_mailbox_history()"
        )
    op.execute("""CREATE FUNCTION protect_shared_email_admission() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'shared email admission is immutable'; END $$""")
    op.execute(
        "CREATE TRIGGER email_admission_immutable BEFORE UPDATE OR DELETE ON email_webhook_admissions "
        "FOR EACH ROW EXECUTE FUNCTION protect_shared_email_admission()"
    )


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM email_mailbox_claims) OR
      EXISTS(SELECT 1 FROM email_mailbox_conflicts) OR EXISTS(SELECT 1 FROM email_webhook_admissions) OR
      EXISTS(SELECT 1 FROM email_execution_activations WHERE provider_binding IS NOT NULL)
      THEN RAISE EXCEPTION 'retained mailbox history prevents lossy downgrade'; END IF; END $$""")
    op.drop_table("email_webhook_admissions")
    op.drop_table("email_mailbox_claims")
    op.drop_table("email_mailbox_conflicts")
    op.execute("DROP FUNCTION protect_email_mailbox_history()")
    op.execute("DROP FUNCTION protect_shared_email_admission()")
    op.drop_column("email_execution_activations", "provider_binding")
