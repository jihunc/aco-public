"""Add human task origin and persistent tenant queue version without rewriting history."""

import sqlalchemy as sa

from alembic import op

revision = "r0011_reviews"
# One additive lineage: accepted storage, communications, then bounded human CRUD.
down_revision = "r0007_communications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("source_action_id", "source_event_id", "invoice_id"):
        op.alter_column("collection_tasks", name, existing_type=sa.UUID(), nullable=True)
    op.add_column("collection_tasks", sa.Column("priority", sa.Integer(), nullable=False, server_default="1"))
    op.create_check_constraint("ck_collection_tasks_priority", "collection_tasks", "priority BETWEEN 0 AND 3")
    op.create_check_constraint(
        "ck_collection_tasks_source_pair",
        "collection_tasks",
        "(source_action_id IS NULL AND source_event_id IS NULL) OR "
        "(source_action_id IS NOT NULL AND source_event_id IS NOT NULL)",
    )
    op.create_table(
        "review_queue_states",
        sa.Column("tenant_id", sa.UUID(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("tenant_id"),
    )
    op.create_index("ix_review_queue_states_tenant_id", "review_queue_states", ["tenant_id"])


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(
        sa.text("SELECT EXISTS(SELECT 1 FROM collection_tasks WHERE source_action_id IS NULL)")
    ):
        raise RuntimeError("cannot downgrade: human task evidence would be lost")
    if connection.scalar(sa.text("SELECT EXISTS(SELECT 1 FROM review_queue_states)")):
        raise RuntimeError("cannot downgrade: review queue evidence would be lost")
    op.drop_index("ix_review_queue_states_tenant_id", "review_queue_states")
    op.drop_table("review_queue_states")
    op.drop_constraint("ck_collection_tasks_source_pair", "collection_tasks", type_="check")
    op.drop_constraint("ck_collection_tasks_priority", "collection_tasks", type_="check")
    op.drop_column("collection_tasks", "priority")
    for name in ("source_action_id", "source_event_id", "invoice_id"):
        op.alter_column("collection_tasks", name, existing_type=sa.UUID(), nullable=False)
