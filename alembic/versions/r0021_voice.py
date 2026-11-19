"""Scoped offline browser voice sessions, genuine action triggers and immutable callback history."""

from alembic import op

revision = "r0021_voice"
down_revision = "r0020_human_handling"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE identity_actors DROP CONSTRAINT identity_actors_check")
    op.execute("""ALTER TABLE identity_actors ADD CONSTRAINT identity_actors_check CHECK (
        (kind='human' AND user_id IS NOT NULL AND user_id=id AND tenant_id IS NULL AND purpose IS NULL) OR
        (kind='service' AND user_id IS NULL AND tenant_id IS NOT NULL AND purpose IS NOT NULL
         AND purpose IN ('durable_worker','voice_callback')))""")
    # Explicit reviewed schema; no application metadata.create_all or vendor-owned DDL.
    common = (
        "id UUID PRIMARY KEY,created_at TIMESTAMPTZ NOT NULL,"
        "updated_at TIMESTAMPTZ NOT NULL,version INTEGER NOT NULL"
    )
    op.execute(f"""CREATE TABLE voice_sessions ({common},tenant_id UUID NOT NULL REFERENCES tenants(id),
      request_id UUID NOT NULL,connection_id UUID NOT NULL,connection_version INTEGER NOT NULL,
      credential_revision VARCHAR NOT NULL,binding_hash VARCHAR NOT NULL,initiator_id UUID NOT NULL
        REFERENCES identity_users(id),
      membership_version INTEGER NOT NULL,tenant_version INTEGER NOT NULL,actor_id UUID NOT NULL
        REFERENCES identity_actors(id),
      actor_version INTEGER NOT NULL,correlation_id UUID NOT NULL,command JSON NOT NULL,expires_at
        TIMESTAMPTZ NOT NULL,
      state VARCHAR NOT NULL,provider_call_id VARCHAR,approval_id UUID REFERENCES approval_requests(id),
      usage_attempt_id UUID REFERENCES usage_attempts(id),UNIQUE(tenant_id,id),UNIQUE(tenant_id,request_id),
      UNIQUE(connection_id,provider_call_id),FOREIGN KEY(tenant_id,connection_id) REFERENCES
        provider_connections(tenant_id,id),
      CHECK(state IN
        ('awaiting_approval','starting','active','connected','completed','failed','unknown_outcome')))""")
    op.execute(f"""CREATE TABLE voice_invoice_links ({common},tenant_id UUID NOT NULL REFERENCES
        tenants(id),
      session_id UUID NOT NULL,invoice_id UUID NOT NULL,source_revision VARCHAR NOT NULL,
      accounting_connection_id UUID NOT NULL REFERENCES provider_connections(id),connection_version
        INTEGER NOT NULL,
      UNIQUE(tenant_id,session_id,invoice_id),FOREIGN KEY(tenant_id,session_id) REFERENCES
        voice_sessions(tenant_id,id),
      FOREIGN KEY(tenant_id,invoice_id) REFERENCES invoice_snapshots(tenant_id,id))""")
    op.execute(f"""CREATE TABLE voice_invocations ({common},tenant_id UUID NOT NULL REFERENCES
        tenants(id),session_id UUID NOT NULL,
      native_id VARCHAR NOT NULL,tool VARCHAR NOT NULL,arguments JSON NOT NULL,result JSON,action_id
        UUID REFERENCES scheduled_actions(id),
      UNIQUE(tenant_id,session_id,id),UNIQUE(session_id,native_id),FOREIGN KEY(tenant_id,session_id)
        REFERENCES voice_sessions(tenant_id,id))""")
    op.execute(f"""CREATE TABLE voice_events ({common},tenant_id UUID NOT NULL REFERENCES
        tenants(id),session_id UUID NOT NULL,
      event_key VARCHAR NOT NULL,kind VARCHAR NOT NULL,raw_sha256 VARCHAR NOT
        NULL,UNIQUE(session_id,event_key),
      FOREIGN KEY(tenant_id,session_id) REFERENCES voice_sessions(tenant_id,id))""")
    op.execute(
        "ALTER TABLE scheduled_actions ADD COLUMN voice_session_id UUID, ADD COLUMN voice_invocation_id UUID"
    )
    op.execute(
        "ALTER TABLE scheduled_actions ADD FOREIGN KEY(tenant_id,voice_session_id) "
        "REFERENCES voice_sessions(tenant_id,id)"
    )
    op.execute("""ALTER TABLE scheduled_actions ADD FOREIGN
        KEY(tenant_id,voice_session_id,voice_invocation_id)
                  REFERENCES voice_invocations(tenant_id,session_id,id)""")
    op.execute("ALTER TABLE scheduled_actions ADD UNIQUE(tenant_id,voice_session_id,id)")
    op.execute(
        "ALTER TABLE collection_tasks ADD COLUMN source_voice_session_id UUID REFERENCES voice_sessions(id)"
    )
    op.execute("""ALTER TABLE collection_tasks ADD FOREIGN
        KEY(tenant_id,source_voice_session_id,source_action_id)
        REFERENCES scheduled_actions(tenant_id,voice_session_id,id)""")
    op.execute("ALTER TABLE collection_tasks DROP CONSTRAINT ck_collection_tasks_source_pair")
    op.execute("""ALTER TABLE collection_tasks ADD CONSTRAINT ck_collection_tasks_source_pair CHECK (
        (((source_action_id IS NULL AND source_event_id IS NULL AND source_agent_run_id IS NULL) OR
          (source_action_id IS NOT NULL AND source_event_id IS NOT NULL AND source_agent_run_id IS NULL) OR
          (source_action_id IS NOT NULL AND source_event_id IS NULL AND source_agent_run_id IS NOT NULL))
          AND source_voice_session_id IS NULL) OR
        (source_action_id IS NOT NULL AND source_voice_session_id IS NOT NULL
          AND source_event_id IS NULL AND source_agent_run_id IS NULL))""")
    # Preserve the previously installed exact trigger predicate without rewriting historical branches.
    op.execute("""DO $$ DECLARE prior TEXT; BEGIN
      SELECT pg_get_constraintdef(oid) INTO prior FROM pg_constraint WHERE
        conrelid='scheduled_actions'::regclass
        AND conname='ck_action_trigger';
      ALTER TABLE scheduled_actions DROP CONSTRAINT ck_action_trigger;
      EXECUTE 'ALTER TABLE scheduled_actions ADD CONSTRAINT ck_action_trigger CHECK ((' ||
        substring(prior FROM 7) || ') AND voice_session_id IS NULL AND voice_invocation_id IS NULL OR '
        || '(kind IN (''voice_session'',''voice_internal'') AND voice_session_id IS NOT NULL '
        || 'AND ((kind=''voice_session'' AND voice_invocation_id IS NULL) OR '
        || '(kind=''voice_internal'' AND voice_invocation_id IS NOT NULL)) '
        || 'AND source_event_id IS NULL AND policy_event_id IS NULL AND policy_activation_id IS NULL '
        || 'AND occurrence_id IS NULL AND policy_version_id IS NULL AND agent_run_id IS NULL '
        || 'AND agent_step_id IS NULL AND promise_event_id IS NULL AND promise_installment_id IS NULL '
        || 'AND reservation_id IS NULL))'; END $$""")
    for table in ("voice_invoice_links", "voice_events"):
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION aco_case_append_only()"
        )
    op.execute("""CREATE FUNCTION aco_voice_session_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'voice source history is immutable'; END IF;
      IF
        (to_jsonb(OLD)-ARRAY['state','provider_call_id','approval_id','usage_attempt_id','version','updated_at'])
        IS DISTINCT FROM
        (to_jsonb(NEW)-ARRAY['state','provider_call_id','approval_id','usage_attempt_id','version','updated_at'])
        OR (OLD.provider_call_id IS NOT NULL AND OLD.provider_call_id IS DISTINCT FROM NEW.provider_call_id)
        OR (OLD.approval_id IS NOT NULL AND OLD.approval_id IS DISTINCT FROM NEW.approval_id)
        OR (OLD.usage_attempt_id IS NOT NULL AND OLD.usage_attempt_id IS DISTINCT FROM NEW.usage_attempt_id)
      THEN RAISE EXCEPTION 'voice authority material is immutable'; END IF; RETURN NEW; END $$""")
    op.execute(
        "CREATE TRIGGER voice_session_immutable BEFORE UPDATE OR DELETE ON voice_sessions "
        "FOR EACH ROW EXECUTE FUNCTION aco_voice_session_immutable()"
    )
    op.execute("""CREATE FUNCTION aco_voice_invocation_immutable() RETURNS trigger LANGUAGE plpgsql AS
        $$ BEGIN
      IF TG_OP='DELETE' OR OLD.result IS NOT NULL THEN RAISE EXCEPTION 'voice tool history is
        immutable'; END IF;
      IF (to_jsonb(OLD)-ARRAY['result','action_id','version','updated_at']) IS DISTINCT FROM
         (to_jsonb(NEW)-ARRAY['result','action_id','version','updated_at']) THEN
         RAISE EXCEPTION 'voice invocation identity is immutable'; END IF; RETURN NEW; END $$""")
    op.execute(
        "CREATE TRIGGER voice_invocation_immutable BEFORE UPDATE OR DELETE ON voice_invocations "
        "FOR EACH ROW EXECUTE FUNCTION aco_voice_invocation_immutable()"
    )


def downgrade() -> None:
    raise RuntimeError("Retained voice history requires an explicit non-lossy downgrade design")
