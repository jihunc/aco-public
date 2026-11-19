"""Generalize immutable cohort definitions for explicitly reviewed observed populations."""

from alembic import op

revision = "r0025_cohort_observed"
down_revision = "r0024_email_live_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE impact_cohort_pairs ADD COLUMN purpose VARCHAR NOT NULL DEFAULT 'ILLUSTRATION' "
        "CHECK(purpose IN ('ILLUSTRATION','OBSERVED'))"
    )
    op.execute("ALTER TABLE impact_cohort_pairs ALTER COLUMN manifest_id DROP NOT NULL")
    op.execute("ALTER TABLE impact_cohort_pairs DROP CONSTRAINT impact_cohort_pairs_tenant_id_key")
    op.execute(
        "CREATE UNIQUE INDEX one_illustrative_cohort_per_tenant "
        "ON impact_cohort_pairs(tenant_id) WHERE purpose='ILLUSTRATION'"
    )
    op.execute(
        "ALTER TABLE invoice_cohorts ADD COLUMN prior_cohort_id UUID, "
        "ADD FOREIGN KEY(tenant_id,prior_cohort_id) REFERENCES invoice_cohorts(tenant_id,id)"
    )
    op.execute("ALTER TABLE invoice_cohorts DROP CONSTRAINT invoice_cohorts_side_check")
    op.execute(
        "ALTER TABLE invoice_cohorts ADD CHECK(side IN ('BASELINE_SYNTHETIC','AFTER_SYNTHETIC','OBSERVED'))"
    )
    op.execute("ALTER TABLE invoice_cohorts DROP CONSTRAINT invoice_cohorts_tenant_id_pair_id_side_key")
    op.execute("ALTER TABLE invoice_cohorts ADD UNIQUE(tenant_id,pair_id,side,version)")


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS(SELECT 1 FROM impact_cohort_pairs WHERE purpose='OBSERVED') THEN "
        "RAISE EXCEPTION 'observed cohort history prevents lossy downgrade'; END IF; END $$"
    )
    op.execute(
        "ALTER TABLE invoice_cohorts DROP CONSTRAINT invoice_cohorts_tenant_id_pair_id_side_version_key"
    )
    op.execute("ALTER TABLE invoice_cohorts ADD UNIQUE(tenant_id,pair_id,side)")
    op.execute("ALTER TABLE invoice_cohorts DROP COLUMN prior_cohort_id")
    op.execute("ALTER TABLE invoice_cohorts DROP CONSTRAINT invoice_cohorts_side_check")
    op.execute("ALTER TABLE invoice_cohorts ADD CHECK(side IN ('BASELINE_SYNTHETIC','AFTER_SYNTHETIC'))")
    op.execute("DROP INDEX one_illustrative_cohort_per_tenant")
    op.execute("ALTER TABLE impact_cohort_pairs ADD UNIQUE(tenant_id)")
    op.execute("ALTER TABLE impact_cohort_pairs ALTER COLUMN manifest_id SET NOT NULL")
    op.execute("ALTER TABLE impact_cohort_pairs DROP COLUMN purpose")
