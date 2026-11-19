"""Genuine graph-owned email action and durable external-action wait state."""

from alembic import op

revision = "r0031_agent_email_dispatch"
down_revision = "r0030_approval_modifications"
branch_labels = None
depends_on = None

EMAIL_TRIGGER = (
    "kind='agent_email'"
    " AND agent_run_id IS NOT NULL"
    " AND agent_step_id IS NOT NULL"
    " AND policy_version_id IS NOT NULL"
    " AND source_event_id IS NULL"
    " AND policy_event_id IS NULL"
    " AND policy_activation_id IS NULL"
    " AND occurrence_id IS NULL"
    " AND promise_event_id IS NULL"
    " AND promise_installment_id IS NULL"
    " AND voice_session_id IS NULL"
    " AND voice_invocation_id IS NULL"
    " AND reservation_id IS NULL"
)


def upgrade() -> None:
    # Retain every previously installed policy/source/promise/voice predicate verbatim.
    quoted = EMAIL_TRIGGER.replace("'", "''")
    op.execute(f"""DO $$ DECLARE prior TEXT; BEGIN
      SELECT pg_get_constraintdef(oid) INTO STRICT prior FROM pg_constraint
      WHERE conrelid='scheduled_actions'::regclass AND conname='ck_action_trigger';
      ALTER TABLE scheduled_actions DROP CONSTRAINT ck_action_trigger;
      EXECUTE 'ALTER TABLE scheduled_actions ADD CONSTRAINT ck_action_trigger CHECK (' ||
        substring(prior FROM 7) || ' OR ({quoted}))';
      END $$""")
    op.drop_constraint("agent_runs_status_check", "agent_runs", type_="check")
    op.create_check_constraint(
        "ck_agent_run_status",
        "agent_runs",
        "status IN ('queued','running','awaiting_approval','awaiting_action','completed',"
        "'partially_completed','failed','cancelled')",
    )


def downgrade() -> None:
    raise RuntimeError("Agent email history requires non-destructive restore")
