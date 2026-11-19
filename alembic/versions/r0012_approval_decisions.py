"""Retain exact human-reviewed state and immutable decision/proposal provenance."""

from alembic import op

revision = "r0012_approval_decisions"
down_revision = "r0010_agent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE approval_decisions ADD COLUMN review_binding JSON NULL")
    op.execute("""CREATE FUNCTION preserve_approval_proposal() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
      IF (to_jsonb(NEW) - ARRAY['updated_at','version','status','granted_at','expires_at','consumed_at'])
         IS DISTINCT FROM
         (to_jsonb(OLD) - ARRAY['updated_at','version','status','granted_at','expires_at','consumed_at']) THEN
        RAISE EXCEPTION 'immutable approval proposal';
      END IF;
      RETURN NEW; END $$""")
    op.execute("""CREATE TRIGGER immutable_approval_proposal BEFORE UPDATE ON approval_requests
      FOR EACH ROW EXECUTE FUNCTION preserve_approval_proposal()""")
    op.execute("""CREATE FUNCTION preserve_approval_decision() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
      RAISE EXCEPTION 'immutable approval decision'; END $$""")
    op.execute("""CREATE TRIGGER immutable_approval_decision BEFORE UPDATE OR DELETE ON approval_decisions
      FOR EACH ROW EXECUTE FUNCTION preserve_approval_decision()""")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM approval_decisions WHERE review_binding IS NOT NULL)
      THEN RAISE EXCEPTION 'approval evidence prevents lossy downgrade'; END IF; END $$""")
    op.execute("DROP TRIGGER immutable_approval_decision ON approval_decisions")
    op.execute("DROP FUNCTION preserve_approval_decision()")
    op.execute("DROP TRIGGER immutable_approval_proposal ON approval_requests")
    op.execute("DROP FUNCTION preserve_approval_proposal()")
    op.execute("ALTER TABLE approval_decisions DROP COLUMN review_binding")
