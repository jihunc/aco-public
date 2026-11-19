"""Bind verified source observations to exact maintained execution authority."""

from alembic import op

revision = "r0017_source_execution_binding"
down_revision = "r0016_promise_performance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing immutable_history rejects UPDATE/DELETE of the entire observation,
    # including this additive evidence. Source provenance and usage bridge are unchanged.
    op.execute("ALTER TABLE accounting_observations ADD COLUMN execution_binding JSON")
    op.create_check_constraint(
        "ck_source_execution_object",
        "accounting_observations",
        "execution_binding IS NULL OR json_typeof(execution_binding) = 'object'",
    )


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS (
      SELECT 1 FROM accounting_observations WHERE execution_binding IS NOT NULL
    ) THEN RAISE EXCEPTION 'source execution evidence prevents lossy downgrade'; END IF; END $$""")
    op.drop_constraint("ck_source_execution_object", "accounting_observations")
    op.drop_column("accounting_observations", "execution_binding")
