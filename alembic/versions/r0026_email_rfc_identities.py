"""Immutable provider-observed RFC identities; preserve all composed originals."""

from alembic import op

revision = "r0026_email_rfc_identities"
down_revision = "r0025_cohort_observed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE communication_rfc_identities (
      id uuid PRIMARY KEY,created_at timestamptz NOT NULL,updated_at timestamptz NOT NULL,
      version integer NOT NULL,tenant_id uuid NOT NULL REFERENCES tenants(id),
      provider_connection_id uuid NOT NULL,communication_id uuid NOT NULL,
      submission_id uuid NOT NULL,usage_event_id uuid NOT NULL UNIQUE,
      provider text NOT NULL,native_message_id text NOT NULL,rfc_message_id text NOT NULL,
      observation_reference text NOT NULL,observation_origin text NOT NULL,
      provider_request_id text,observed_at timestamptz NOT NULL,
      native_identity text NOT NULL,synthetic boolean NOT NULL,
      UNIQUE(tenant_id,id),
      FOREIGN KEY(tenant_id,provider_connection_id) REFERENCES provider_connections(tenant_id,id),
      FOREIGN KEY(tenant_id,communication_id) REFERENCES communications(tenant_id,id),
      FOREIGN KEY(tenant_id,submission_id) REFERENCES email_submissions(tenant_id,id),
      FOREIGN KEY(tenant_id,usage_event_id) REFERENCES provider_usage_events(tenant_id,id),
      CHECK(length(rfc_message_id) BETWEEN 5 AND 998),
      CHECK(observation_origin IN ('PROVIDER','APPLICATION')),
      CHECK(native_identity IN ('PROVIDER','SIMULATED')),
      CHECK(observation_origin='PROVIDER' OR provider_request_id IS NULL),
      CHECK((synthetic AND native_identity='SIMULATED') OR
            (NOT synthetic AND native_identity='PROVIDER')))""")
    op.execute(
        "CREATE INDEX ix_communication_rfc_lookup ON communication_rfc_identities "
        "(tenant_id,provider_connection_id,rfc_message_id)"
    )
    op.execute("""CREATE FUNCTION protect_communication_rfc_identity() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'provider-observed RFC identity history is immutable'; END $$""")
    op.execute(
        "CREATE TRIGGER communication_rfc_identity_immutable BEFORE UPDATE OR DELETE "
        "ON communication_rfc_identities FOR EACH ROW "
        "EXECUTE FUNCTION protect_communication_rfc_identity()"
    )


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM communication_rfc_identities) THEN
      RAISE EXCEPTION 'retained RFC identity history prevents lossy downgrade'; END IF; END $$""")
    op.drop_table("communication_rfc_identities")
    op.execute("DROP FUNCTION protect_communication_rfc_identity()")
