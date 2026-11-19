"""Immutable human material-edit lineage; retain old proposals and decisions."""

import sqlalchemy as sa

from alembic import op

revision = "r0030_approval_modifications"
down_revision = "r0029_invoice_customer_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("approval_requests", sa.Column("supersedes_id", sa.UUID(), nullable=True))
    op.add_column("approval_requests", sa.Column("editor_id", sa.UUID(), nullable=True))
    op.add_column("approval_requests", sa.Column("edit_reason", sa.String(), nullable=True))
    op.create_foreign_key(
        "fk_approval_modification_parent",
        "approval_requests",
        "approval_requests",
        ["tenant_id", "supersedes_id"],
        ["tenant_id", "id"],
    )
    op.create_foreign_key(
        "fk_approval_modification_editor", "approval_requests", "identity_actors", ["editor_id"], ["id"]
    )
    op.create_unique_constraint(
        "uq_approval_modification_successor", "approval_requests", ["tenant_id", "supersedes_id"]
    )
    op.create_check_constraint(
        "ck_approval_modification_lineage",
        "approval_requests",
        "(supersedes_id IS NULL AND editor_id IS NULL AND edit_reason IS NULL) OR "
        "(supersedes_id IS NOT NULL AND editor_id IS NOT NULL AND edit_reason IS NOT NULL)",
    )
    # Existing preserve_approval_proposal compares every non-lifecycle column,
    # including these additions; both update and delete remain prohibited.


def downgrade() -> None:
    raise RuntimeError("Approval modification history requires non-destructive restore")
