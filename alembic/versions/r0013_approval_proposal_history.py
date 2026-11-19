"""Preserve pending original proposals as well as decided approval evidence."""

from alembic import op

revision = "r0013_approval_proposal_history"
down_revision = "r0012_approval_decisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE OR REPLACE FUNCTION preserve_approval_proposal() RETURNS trigger
      LANGUAGE plpgsql AS $$ BEGIN
      IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'immutable approval proposal';
      END IF;
      IF (to_jsonb(NEW) - ARRAY['updated_at','version','status','granted_at','expires_at','consumed_at'])
         IS DISTINCT FROM
         (to_jsonb(OLD) - ARRAY['updated_at','version','status','granted_at','expires_at','consumed_at']) THEN
        RAISE EXCEPTION 'immutable approval proposal';
      END IF;
      RETURN NEW; END $$""")
    op.execute("DROP TRIGGER immutable_approval_proposal ON approval_requests")
    op.execute("""CREATE TRIGGER immutable_approval_proposal BEFORE UPDATE OR DELETE ON approval_requests
      FOR EACH ROW EXECUTE FUNCTION preserve_approval_proposal()""")


def downgrade() -> None:
    # Revert trigger coverage only; never remove retained proposal or decision rows.
    op.execute("DROP TRIGGER immutable_approval_proposal ON approval_requests")
    op.execute("""CREATE TRIGGER immutable_approval_proposal BEFORE UPDATE ON approval_requests
      FOR EACH ROW EXECUTE FUNCTION preserve_approval_proposal()""")
