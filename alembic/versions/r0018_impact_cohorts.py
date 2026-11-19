"""Freeze synthetic source cohorts and retain every computed result version."""

from alembic import op

revision = "r0018_impact_cohorts"
down_revision = "r0017_source_execution_binding"
branch_labels = None
depends_on = None

TABLES = (
    "impact_cohort_pairs",
    "invoice_cohorts",
    "invoice_cohort_memberships",
    "impact_cohort_results",
    "impact_cohort_refreshes",
)
COMMON = (
    "id UUID PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL, "
    "updated_at TIMESTAMPTZ NOT NULL, version INTEGER NOT NULL"
)


def upgrade() -> None:
    op.execute(f"""CREATE TABLE impact_cohort_pairs (
      {COMMON}, tenant_id UUID NOT NULL REFERENCES tenants(id), provider_connection_id UUID NOT NULL,
      actor_id UUID NOT NULL REFERENCES identity_actors(id), request_id UUID NOT NULL,
      command JSON NOT NULL, manifest_id UUID NOT NULL,
      FOREIGN KEY(tenant_id,provider_connection_id,manifest_id)
      REFERENCES demo_source_records(tenant_id,provider_connection_id,id),
      UNIQUE(tenant_id), UNIQUE(tenant_id,id), UNIQUE(tenant_id,request_id),
      FOREIGN KEY(tenant_id,provider_connection_id) REFERENCES provider_connections(tenant_id,id))""")
    op.execute(f"""CREATE TABLE invoice_cohorts (
      {COMMON}, tenant_id UUID NOT NULL REFERENCES tenants(id), pair_id UUID NOT NULL,
      side VARCHAR NOT NULL CHECK(side IN ('BASELINE_SYNTHETIC','AFTER_SYNTHETIC')), definition JSON NOT NULL,
      UNIQUE(tenant_id,id), UNIQUE(tenant_id,pair_id,side),
      FOREIGN KEY(tenant_id,pair_id) REFERENCES impact_cohort_pairs(tenant_id,id))""")
    op.execute(f"""CREATE TABLE invoice_cohort_memberships (
      {COMMON}, tenant_id UUID NOT NULL REFERENCES tenants(id),
      cohort_id UUID NOT NULL, invoice_id UUID NOT NULL,
      basis JSON NOT NULL, UNIQUE(tenant_id,cohort_id,invoice_id),
      FOREIGN KEY(tenant_id,cohort_id) REFERENCES invoice_cohorts(tenant_id,id),
      FOREIGN KEY(tenant_id,invoice_id) REFERENCES invoice_snapshots(tenant_id,id))""")
    op.execute(f"""CREATE TABLE impact_cohort_results (
      {COMMON}, tenant_id UUID NOT NULL REFERENCES tenants(id), cohort_id UUID NOT NULL,
      prior_result_id UUID, evidence JSON NOT NULL, result JSON NOT NULL,
      UNIQUE(tenant_id,cohort_id,id), UNIQUE(tenant_id,cohort_id,version), CHECK(version > 0),
      FOREIGN KEY(tenant_id,cohort_id) REFERENCES invoice_cohorts(tenant_id,id),
      FOREIGN KEY(tenant_id,cohort_id,prior_result_id)
      REFERENCES impact_cohort_results(tenant_id,cohort_id,id))""")
    op.execute(f"""CREATE TABLE impact_cohort_refreshes (
      {COMMON}, tenant_id UUID NOT NULL REFERENCES tenants(id), pair_id UUID NOT NULL,
      request_id UUID NOT NULL, actor_id UUID NOT NULL REFERENCES identity_actors(id), command JSON NOT NULL,
      UNIQUE(tenant_id,request_id),
      FOREIGN KEY(tenant_id,pair_id) REFERENCES impact_cohort_pairs(tenant_id,id))""")
    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION aco_case_append_only()"
        )


def downgrade() -> None:
    for table in TABLES:
        op.execute(
            f"DO $$ BEGIN IF EXISTS(SELECT 1 FROM {table}) THEN "
            "RAISE EXCEPTION 'cohort history prevents lossy downgrade'; END IF; END $$"
        )
    for table in reversed(TABLES):
        op.drop_table(table)
