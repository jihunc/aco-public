"""Retained signed operational voice facts, exclusive from communication/media sources."""

import sqlalchemy as sa

from alembic import op

revision = "r0032_voice_business_evidence"
down_revision = "r0031_agent_email_dispatch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("uq_voice_event_scope_id", "voice_events", ["tenant_id", "session_id", "id"])
    op.execute("""CREATE TABLE voice_business_evidence (
      id uuid PRIMARY KEY,created_at timestamptz NOT NULL,updated_at timestamptz NOT NULL,
      version integer NOT NULL,tenant_id uuid NOT NULL REFERENCES tenants(id),
      session_id uuid NOT NULL,invocation_id uuid NOT NULL,source_event_id uuid NOT NULL,
      action_id uuid NOT NULL,provider_connection_id uuid NOT NULL,
      accounting_connection_id uuid NOT NULL,customer_id uuid NOT NULL,
      provider_call_id text NOT NULL,native_invocation_id text NOT NULL,
      fact_kind text NOT NULL,facts json NOT NULL,facts_hash text NOT NULL,
      synthetic boolean NOT NULL,evidence_mode text NOT NULL,source_state text NOT NULL,
      UNIQUE(tenant_id,id),UNIQUE(tenant_id,session_id,invocation_id),
      FOREIGN KEY(tenant_id,session_id,invocation_id) REFERENCES voice_invocations(tenant_id,session_id,id),
      FOREIGN KEY(tenant_id,session_id,source_event_id) REFERENCES voice_events(tenant_id,session_id,id),
      FOREIGN KEY(tenant_id,action_id) REFERENCES scheduled_actions(tenant_id,id),
      FOREIGN KEY(tenant_id,provider_connection_id) REFERENCES provider_connections(tenant_id,id),
      FOREIGN KEY(tenant_id,accounting_connection_id,customer_id)
        REFERENCES customers(tenant_id,provider_connection_id,id),
      CHECK(fact_kind IN ('case','promise','contact_stop')),
      CHECK(source_state='VOICE_OPERATIONAL_METADATA'),
      CHECK(synthetic AND evidence_mode='MOCKED_HTTP'),
      CHECK(facts_hash ~ '^[0-9a-f]{64}$'))""")
    op.create_index("ix_voice_business_evidence_tenant_id", "voice_business_evidence", ["tenant_id"])
    op.execute("""CREATE FUNCTION validate_voice_business_source() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
      IF NOT EXISTS (
        SELECT 1 FROM voice_invocations i
        JOIN voice_sessions s ON s.tenant_id=i.tenant_id AND s.id=i.session_id
        JOIN scheduled_actions a ON a.tenant_id=i.tenant_id AND a.id=i.action_id
        JOIN invoice_snapshots n ON n.tenant_id=a.tenant_id AND n.id=a.invoice_id
        JOIN voice_events e ON e.tenant_id=i.tenant_id AND e.session_id=i.session_id
        WHERE i.tenant_id=NEW.tenant_id AND i.session_id=NEW.session_id AND i.id=NEW.invocation_id
          AND i.result IS NULL AND a.id=NEW.action_id AND a.kind='voice_internal'
          AND a.status='executing' AND a.voice_session_id=s.id AND a.voice_invocation_id=i.id
          AND a.command::jsonb=i.arguments::jsonb
          AND s.state IN ('active','connected') AND s.expires_at>clock_timestamp()
          AND s.connection_id=NEW.provider_connection_id AND s.provider_call_id=NEW.provider_call_id
          AND i.native_id=NEW.native_invocation_id AND e.id=NEW.source_event_id
          AND e.kind='tool.requested' AND e.event_key='tool:'||i.native_id
          AND n.customer_id=NEW.customer_id AND n.provider_connection_id=NEW.accounting_connection_id
          AND ((NEW.fact_kind='case' AND i.tool='upsert_case'
                 AND i.arguments::jsonb->'case_facts'=NEW.facts::jsonb)
            OR (NEW.fact_kind='promise' AND i.tool='record_promise'
                 AND i.arguments::jsonb->'promise_facts'=NEW.facts::jsonb)
            OR (NEW.fact_kind='contact_stop' AND i.tool='request_collection_hold'
                 AND i.arguments::jsonb->'hold_facts'=NEW.facts::jsonb))
      ) THEN RAISE EXCEPTION 'voice operational source requires exact active signed invocation'; END IF;
      RETURN NEW; END $$""")
    op.execute(
        "CREATE TRIGGER voice_business_source_valid BEFORE INSERT ON voice_business_evidence "
        "FOR EACH ROW EXECUTE FUNCTION validate_voice_business_source()"
    )
    op.execute(
        "CREATE TRIGGER voice_business_source_immutable BEFORE UPDATE OR DELETE "
        "ON voice_business_evidence FOR EACH ROW EXECUTE FUNCTION aco_case_append_only()"
    )
    op.alter_column("case_evidence", "communication_id", existing_type=sa.UUID(), nullable=True)
    op.add_column("case_evidence", sa.Column("voice_evidence_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_case_voice_evidence",
        "case_evidence",
        "voice_business_evidence",
        ["tenant_id", "voice_evidence_id"],
        ["tenant_id", "id"],
    )
    op.create_check_constraint(
        "ck_case_evidence_source",
        "case_evidence",
        "(communication_id IS NOT NULL) <> (voice_evidence_id IS NOT NULL)",
    )
    op.alter_column("payment_promises", "source_communication_id", existing_type=sa.UUID(), nullable=True)
    for column in ("source_voice_evidence_id", "confirmation_voice_evidence_id"):
        op.add_column("payment_promises", sa.Column(column, sa.UUID(), nullable=True))
        op.create_foreign_key(
            "fk_promise_" + column,
            "payment_promises",
            "voice_business_evidence",
            ["tenant_id", column],
            ["tenant_id", "id"],
        )
    op.create_check_constraint(
        "ck_promise_source_kind",
        "payment_promises",
        "(source_communication_id IS NOT NULL) <> (source_voice_evidence_id IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_promise_confirmation_source",
        "payment_promises",
        "confirmation_communication_id IS NULL OR confirmation_voice_evidence_id IS NULL",
    )
    op.execute("""CREATE FUNCTION preserve_promise_source_identity() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN IF OLD.source_communication_id IS DISTINCT FROM NEW.source_communication_id OR
        OLD.source_voice_evidence_id IS DISTINCT FROM NEW.source_voice_evidence_id THEN
        RAISE EXCEPTION 'original promise source identity is immutable'; END IF; RETURN NEW; END $$""")
    op.execute(
        "CREATE TRIGGER promise_source_identity_immutable BEFORE UPDATE ON payment_promises "
        "FOR EACH ROW EXECUTE FUNCTION preserve_promise_source_identity()"
    )


def downgrade() -> None:
    raise RuntimeError("Voice business evidence requires non-destructive restore")
