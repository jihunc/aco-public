"""Immutable explicit Voice authority and private existing-call join capabilities; no grants seeded."""

from alembic import op

revision = "r0033_voice_live_grants"
down_revision = "r0032_voice_business_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE voice_live_grants (
      id uuid PRIMARY KEY,created_at timestamptz NOT NULL,updated_at timestamptz NOT NULL,
      version integer NOT NULL,tenant_id uuid NOT NULL REFERENCES tenants(id),
      tenant_generation integer NOT NULL,initiator_id uuid NOT NULL REFERENCES identity_users(id),
      membership_version integer NOT NULL,actor_version integer NOT NULL,
      participant_id uuid NOT NULL REFERENCES identity_users(id),
      participant_membership_version integer NOT NULL,participant_actor_version integer NOT NULL,
      connection_id uuid NOT NULL,connection_version integer NOT NULL,credential_revision text NOT NULL,
      policy_version_id uuid NOT NULL,policy_generation integer NOT NULL,
      authorization_ref text NOT NULL UNIQUE,material json NOT NULL,source_scope json NOT NULL,
      scope_ids json NOT NULL,revoked_at timestamptz,UNIQUE(tenant_id,id),
      FOREIGN KEY(tenant_id,connection_id) REFERENCES provider_connections(tenant_id,id),
      FOREIGN KEY(tenant_id,policy_version_id) REFERENCES collection_policy_versions(tenant_id,id))""")
    op.execute("""CREATE FUNCTION protect_voice_live_grant() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
      IF TG_OP='DELETE' OR
        (to_jsonb(NEW)-ARRAY['revoked_at','updated_at','version']) IS DISTINCT FROM
        (to_jsonb(OLD)-ARRAY['revoked_at','updated_at','version']) OR
        (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at)
      THEN RAISE EXCEPTION 'voice live grant authority is immutable'; END IF;
      RETURN NEW; END $$""")
    op.execute(
        "CREATE TRIGGER voice_live_grant_immutable BEFORE UPDATE OR DELETE ON voice_live_grants "
        "FOR EACH ROW EXECUTE FUNCTION protect_voice_live_grant()"
    )
    # r32 source rows remain unchanged; extend only the exact source-mode coherence check.
    op.execute("""DO $$ DECLARE old_name text; BEGIN
      SELECT conname INTO STRICT old_name FROM pg_constraint
      WHERE conrelid='voice_business_evidence'::regclass AND contype='c'
        AND pg_get_constraintdef(oid) LIKE '%evidence_mode%';
      EXECUTE format('ALTER TABLE voice_business_evidence DROP CONSTRAINT %I', old_name);
      END $$""")
    op.execute(
        "ALTER TABLE voice_business_evidence ADD CONSTRAINT ck_voice_business_mode "
        "CHECK ((synthetic AND evidence_mode='MOCKED_HTTP') OR "
        "(NOT synthetic AND evidence_mode='LIVE'))"
    )
    op.execute("ALTER TABLE voice_sessions ADD COLUMN live_grant_id uuid")
    op.execute(
        "ALTER TABLE voice_sessions ADD CONSTRAINT fk_voice_live_grant "
        "FOREIGN KEY(tenant_id,live_grant_id) REFERENCES voice_live_grants(tenant_id,id)"
    )
    op.execute("""CREATE TABLE voice_live_requests (
      id uuid PRIMARY KEY,created_at timestamptz NOT NULL,updated_at timestamptz NOT NULL,
      version integer NOT NULL,tenant_id uuid NOT NULL REFERENCES tenants(id),
      grant_id uuid NOT NULL,session_id uuid,attempt_id uuid NOT NULL UNIQUE,reservation_id uuid NOT NULL,
      operation text NOT NULL CHECK(operation IN
        ('probe_assistant','probe_credential','start_browser','get_call','end_call')),
      FOREIGN KEY(tenant_id,grant_id) REFERENCES voice_live_grants(tenant_id,id),
      FOREIGN KEY(tenant_id,session_id) REFERENCES voice_sessions(tenant_id,id),
      FOREIGN KEY(tenant_id,attempt_id) REFERENCES usage_attempts(tenant_id,id),
      FOREIGN KEY(tenant_id,reservation_id) REFERENCES budget_reservations(tenant_id,id))""")
    op.execute(
        "CREATE UNIQUE INDEX uq_voice_live_single_create ON voice_live_requests(tenant_id,session_id) "
        "WHERE operation='start_browser'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_voice_live_single_stop ON voice_live_requests(tenant_id,session_id) "
        "WHERE operation='end_call'"
    )
    op.execute("""CREATE TABLE voice_browser_joins (
      id uuid PRIMARY KEY,created_at timestamptz NOT NULL,updated_at timestamptz NOT NULL,
      version integer NOT NULL,tenant_id uuid NOT NULL REFERENCES tenants(id),
      grant_id uuid NOT NULL,session_id uuid NOT NULL,provider_call_id text NOT NULL,
      encrypted_material bytea NOT NULL,expires_at timestamptz NOT NULL,UNIQUE(tenant_id,session_id),
      FOREIGN KEY(tenant_id,grant_id) REFERENCES voice_live_grants(tenant_id,id),
      FOREIGN KEY(tenant_id,session_id) REFERENCES voice_sessions(tenant_id,id))""")
    op.execute("""CREATE TABLE voice_live_profiles (
      id uuid PRIMARY KEY,created_at timestamptz NOT NULL,updated_at timestamptz NOT NULL,
      version integer NOT NULL,tenant_id uuid NOT NULL REFERENCES tenants(id),grant_id uuid NOT NULL UNIQUE,
      connection_version integer NOT NULL,binding_sha256 text NOT NULL,
      assistant_profile_sha256 text NOT NULL,assistant_usage_event_id uuid NOT NULL,
      credential_usage_event_id uuid NOT NULL,
      FOREIGN KEY(tenant_id,grant_id) REFERENCES voice_live_grants(tenant_id,id),
      FOREIGN KEY(tenant_id,assistant_usage_event_id) REFERENCES provider_usage_events(tenant_id,id),
      FOREIGN KEY(tenant_id,credential_usage_event_id) REFERENCES provider_usage_events(tenant_id,id))""")
    for table in ("voice_live_requests", "voice_browser_joins", "voice_live_profiles"):
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION aco_case_append_only()"
        )


def downgrade() -> None:
    raise RuntimeError("Retained Voice grant, request and join authority requires non-destructive restore")
