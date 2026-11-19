"""Bind fake inbound investigations to their real captured receipt and activation."""

from alembic import op

revision = "r0015_agent_ingress"
down_revision = "r0014_email_ingress"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_runs ADD COLUMN source_receipt_id UUID, "
        "ADD COLUMN source_communication_id UUID, ADD COLUMN source_activation_id UUID"
    )
    for suffix, column, target in (
        ("receipt", "source_receipt_id", "email_ingress_receipts"),
        ("communication", "source_communication_id", "communications"),
        ("activation", "source_activation_id", "email_ingress_activations"),
    ):
        op.create_foreign_key(
            "fk_agent_ingress_" + suffix, "agent_runs", target, ["tenant_id", column], ["tenant_id", "id"]
        )
    op.create_unique_constraint("uq_agent_ingress_receipt", "agent_runs", ["tenant_id", "source_receipt_id"])
    op.create_check_constraint(
        "ck_agent_ingress_fake_scope",
        "agent_runs",
        "(source_receipt_id IS NULL AND source_communication_id IS NULL "
        "AND source_activation_id IS NULL) OR "
        "(source_receipt_id IS NOT NULL AND source_communication_id IS NOT NULL "
        "AND source_activation_id IS NOT NULL "
        "AND authenticity = 'FAKE')",
    )


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM agent_runs WHERE source_receipt_id IS NOT NULL)
      THEN RAISE EXCEPTION 'inbound run evidence prevents lossy downgrade'; END IF; END $$""")
    for name in (
        "fk_agent_ingress_receipt",
        "fk_agent_ingress_communication",
        "fk_agent_ingress_activation",
        "uq_agent_ingress_receipt",
        "ck_agent_ingress_fake_scope",
    ):
        op.drop_constraint(name, "agent_runs")
    for name in ("source_receipt_id", "source_communication_id", "source_activation_id"):
        op.drop_column("agent_runs", name)
