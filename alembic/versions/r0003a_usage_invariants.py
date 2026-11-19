"""Preserve local identity audit classification and database cost invariants."""

import sqlalchemy as sa

from alembic import op

revision = "r0003a_usage_invariants"
down_revision = "r0003_usage"
branch_labels = None
depends_on = None

AMOUNTS = {
    "usage_revisions": ("cost_amount",),
    "usage_rates": ("amount",),
    "budget_limits": ("amount",),
    "budget_reservations": ("original_amount", "retained_amount", "released_amount", "spent_amount"),
}


def upgrade() -> None:
    # No historical field is rewritten; NULL means request activity is unknown.
    op.add_column("identity_provider_events", sa.Column("provider_attempted", sa.Boolean(), nullable=True))
    op.execute("DROP TRIGGER identity_usage ON identity_provider_events")
    op.execute("""CREATE TRIGGER identity_usage AFTER INSERT OR UPDATE ON identity_provider_events
        FOR EACH ROW WHEN (NEW.provider_attempted IS DISTINCT FROM false)
        EXECUTE FUNCTION bridge_identity_usage()""")
    op.create_check_constraint(
        "quality_amount",
        "usage_revisions",
        "(cost_quality = 'UNKNOWN' AND cost_amount IS NULL) OR "
        "(cost_quality IN ('PROVIDER_REPORTED','RATE_ESTIMATE') AND cost_amount IS NOT NULL)",
    )
    for table, columns in AMOUNTS.items():
        for column in columns:
            op.create_check_constraint(
                f"finite_{column}", table, f"{column} >= 0 AND {column} < 100000000000000000000000000"
            )
    op.execute("""CREATE FUNCTION enforce_usage_currency() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE scope text;
        BEGIN
            SELECT scope_kind INTO STRICT scope FROM provider_usage_events WHERE id = NEW.event_id;
            IF NEW.currency IS NULL AND (scope <> 'GLOBAL_IDENTITY' OR NEW.cost_quality <> 'UNKNOWN'
                OR NEW.cost_amount IS NOT NULL) THEN
                RAISE EXCEPTION 'currency unavailable only for unpriced global identity';
            END IF;
            IF NEW.currency IS NOT NULL AND NEW.currency !~ '^[A-Z]{3}$' THEN
                RAISE EXCEPTION 'invalid provider currency';
            END IF;
            RETURN NEW;
        END $$""")
    op.execute("""CREATE TRIGGER usage_currency BEFORE INSERT ON usage_revisions
        FOR EACH ROW EXECUTE FUNCTION enforce_usage_currency()""")


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM identity_provider_events WHERE provider_attempted IS NOT NULL) THEN
            RAISE EXCEPTION 'cannot downgrade: identity activity classification would be lost';
        END IF;
    END $$""")
    op.drop_constraint("quality_amount", "usage_revisions", type_="check")
    op.execute("DROP TRIGGER usage_currency ON usage_revisions")
    op.execute("DROP FUNCTION enforce_usage_currency()")
    for table, columns in AMOUNTS.items():
        for column in columns:
            op.drop_constraint(f"finite_{column}", table, type_="check")
    op.execute("DROP TRIGGER identity_usage ON identity_provider_events")
    op.execute("""CREATE TRIGGER identity_usage AFTER INSERT OR UPDATE ON identity_provider_events
        FOR EACH ROW EXECUTE FUNCTION bridge_identity_usage()""")
    op.drop_column("identity_provider_events", "provider_attempted")
