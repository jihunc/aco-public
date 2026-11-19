"""Explicit synthetic consent and append-only withdrawal evidence."""

from alembic import op

revision = "r0027_voice_consent"
down_revision = "r0026_email_rfc_identities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""ALTER TABLE artifact_purge_requests
      ADD COLUMN job_id BIGINT, ADD COLUMN job_version INTEGER NOT NULL DEFAULT 1,
      ADD COLUMN worker_actor_id UUID REFERENCES identity_actors(id), ADD COLUMN lease_token UUID,
      ADD COLUMN lease_until TIMESTAMPTZ""")
    op.execute("""CREATE TABLE consent_evidence (
      id UUID PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
      version INTEGER NOT NULL CHECK(version > 0), tenant_id UUID NOT NULL REFERENCES tenants(id),
      request_id UUID NOT NULL, customer_id UUID NOT NULL, accounting_connection_id UUID NOT NULL,
      voice_connection_id UUID NOT NULL, voice_connection_version INTEGER NOT NULL,
      storage_connection_id UUID, storage_connection_version INTEGER,
      recorded_by UUID NOT NULL REFERENCES identity_users(id), membership_version INTEGER NOT NULL,
      tenant_version INTEGER NOT NULL, command JSON NOT NULL, material_hash VARCHAR NOT NULL,
      expires_at TIMESTAMPTZ NOT NULL, UNIQUE(tenant_id,id), UNIQUE(tenant_id,request_id),
      FOREIGN KEY(tenant_id,accounting_connection_id,customer_id)
        REFERENCES customers(tenant_id,provider_connection_id,id),
      FOREIGN KEY(tenant_id,voice_connection_id) REFERENCES provider_connections(tenant_id,id),
      FOREIGN KEY(tenant_id,storage_connection_id) REFERENCES provider_connections(tenant_id,id))""")
    op.execute("""CREATE TABLE consent_revocations (
      id UUID PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
      version INTEGER NOT NULL CHECK(version > 0), tenant_id UUID NOT NULL REFERENCES tenants(id),
      consent_id UUID NOT NULL, request_id UUID NOT NULL,
      actor_id UUID NOT NULL REFERENCES identity_users(id), command JSON NOT NULL,
      UNIQUE(tenant_id,consent_id), UNIQUE(tenant_id,request_id),
      FOREIGN KEY(tenant_id,consent_id) REFERENCES consent_evidence(tenant_id,id))""")
    for table in ("consent_evidence", "consent_revocations"):
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION aco_case_append_only()"
        )

    op.execute("""CREATE TABLE voice_artifact_links (
      id UUID PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
      version INTEGER NOT NULL CHECK(version > 0), tenant_id UUID NOT NULL REFERENCES tenants(id),
      session_id UUID NOT NULL, consent_id UUID NOT NULL, artifact_id UUID NOT NULL UNIQUE,
      kind VARCHAR NOT NULL CHECK(kind IN ('transcript','recording')), native_reference VARCHAR NOT NULL,
      provider_purged_at TIMESTAMPTZ, provider_purge_receipt VARCHAR,
      UNIQUE(tenant_id,session_id,kind),
      FOREIGN KEY(tenant_id,session_id) REFERENCES voice_sessions(tenant_id,id),
      FOREIGN KEY(tenant_id,consent_id) REFERENCES consent_evidence(tenant_id,id),
      FOREIGN KEY(tenant_id,artifact_id) REFERENCES artifacts(tenant_id,id))""")
    op.execute("""CREATE TABLE voice_post_calls (
      id UUID PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
      version INTEGER NOT NULL CHECK(version > 0), tenant_id UUID NOT NULL REFERENCES tenants(id),
      session_id UUID NOT NULL, communication_id UUID NOT NULL UNIQUE,
      UNIQUE(tenant_id,session_id),
      FOREIGN KEY(tenant_id,session_id) REFERENCES voice_sessions(tenant_id,id),
      FOREIGN KEY(tenant_id,communication_id) REFERENCES communications(tenant_id,id))""")
    for table in ("voice_artifact_links", "voice_post_calls"):
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
    op.execute(
        "CREATE TRIGGER voice_post_call_immutable BEFORE UPDATE OR DELETE ON voice_post_calls "
        "FOR EACH ROW EXECUTE FUNCTION aco_case_append_only()"
    )
    op.execute("""CREATE FUNCTION aco_voice_artifact_identity() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'voice artifact history is retained'; END IF;
      IF ROW(OLD.id,OLD.tenant_id,OLD.session_id,OLD.consent_id,OLD.artifact_id,OLD.kind,OLD.native_reference)
        IS DISTINCT FROM ROW(NEW.id,NEW.tenant_id,NEW.session_id,NEW.consent_id,
                             NEW.artifact_id,NEW.kind,NEW.native_reference)
      THEN RAISE EXCEPTION 'voice artifact identity is immutable'; END IF;
      IF OLD.provider_purged_at IS NOT NULL AND ROW(OLD.provider_purged_at,OLD.provider_purge_receipt)
        IS DISTINCT FROM ROW(NEW.provider_purged_at,NEW.provider_purge_receipt)
      THEN RAISE EXCEPTION 'provider purge receipt is immutable'; END IF;
      RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER voice_artifact_identity BEFORE UPDATE OR DELETE ON voice_artifact_links "
        "FOR EACH ROW EXECUTE FUNCTION aco_voice_artifact_identity()"
    )

    op.execute("""CREATE TABLE artifact_privacy_events (
      id UUID PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
      version INTEGER NOT NULL CHECK(version > 0), tenant_id UUID NOT NULL REFERENCES tenants(id),
      artifact_id UUID NOT NULL, request_id UUID NOT NULL,
      actor_id UUID NOT NULL REFERENCES identity_users(id),
      kind VARCHAR NOT NULL, parent_id UUID, command JSON NOT NULL,
      UNIQUE(tenant_id,id), UNIQUE(tenant_id,request_id),
      FOREIGN KEY(tenant_id,artifact_id) REFERENCES artifacts(tenant_id,id),
      FOREIGN KEY(tenant_id,parent_id) REFERENCES artifact_privacy_events(tenant_id,id))""")
    op.create_index("ix_artifact_privacy_events_tenant_id", "artifact_privacy_events", ["tenant_id"])
    op.execute(
        "CREATE TRIGGER artifact_privacy_immutable BEFORE UPDATE OR DELETE ON artifact_privacy_events "
        "FOR EACH ROW EXECUTE FUNCTION aco_case_append_only()"
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS(SELECT 1 FROM consent_evidence) OR EXISTS(SELECT 1 FROM voice_post_calls) "
        "OR EXISTS(SELECT 1 FROM artifact_privacy_events) "
        "OR EXISTS(SELECT 1 FROM artifact_purge_requests WHERE job_id IS NOT NULL) THEN "
        "RAISE EXCEPTION 'consent history prevents lossy downgrade'; END IF; END $$"
    )
    op.execute("""ALTER TABLE artifact_purge_requests DROP COLUMN job_id, DROP COLUMN job_version,
      DROP COLUMN worker_actor_id, DROP COLUMN lease_token, DROP COLUMN lease_until""")
    op.drop_table("artifact_privacy_events")
    op.drop_table("voice_post_calls")
    op.drop_table("voice_artifact_links")
    op.execute("DROP FUNCTION aco_voice_artifact_identity()")
    op.drop_table("consent_revocations")
    op.drop_table("consent_evidence")
