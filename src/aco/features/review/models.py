"""Canonical review entities, extended by T16 human CRUD/decisions/reordering."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Numeric,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from aco.features.identity.models import Base, Record


class CollectionTask(Record, Base):
    __tablename__ = "collection_tasks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_action_id"),
        ForeignKeyConstraint(
            ["tenant_id", "source_voice_session_id", "source_action_id"],
            ["scheduled_actions.tenant_id", "scheduled_actions.voice_session_id", "scheduled_actions.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "source_action_id"], ["scheduled_actions.tenant_id", "scheduled_actions.id"]
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    source_action_id: Mapped[UUID | None] = mapped_column(ForeignKey("scheduled_actions.id"))
    source_voice_session_id: Mapped[UUID | None] = mapped_column(ForeignKey("voice_sessions.id"))
    source_agent_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("agent_runs.id"))
    source_event_id: Mapped[UUID | None] = mapped_column(ForeignKey("accounting_events.id"))
    invoice_id: Mapped[UUID | None] = mapped_column(ForeignKey("invoice_snapshots.id"))
    creator_id: Mapped[UUID] = mapped_column(ForeignKey("identity_actors.id"))
    assignee_id: Mapped[UUID | None] = mapped_column(ForeignKey("identity_users.id"))
    title: Mapped[str]
    notes: Mapped[str] = mapped_column(default="")
    status: Mapped[str] = mapped_column(default="open")
    priority: Mapped[int] = mapped_column(default=1)
    priority_rank: Mapped[Decimal] = mapped_column(Numeric(30, 10), default=Decimal(0))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    command: Mapped[dict[str, object]] = mapped_column(JSON)


class ReviewQueueState(Record, Base):
    __tablename__ = "review_queue_states"
    __table_args__ = (UniqueConstraint("tenant_id"),)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)


class ApprovalRequest(Record, Base):
    __tablename__ = "approval_requests"
    __table_args__ = (
        UniqueConstraint("tenant_id", "action_id", "action_version", "material_hash"),
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "supersedes_id"),
        ForeignKeyConstraint(
            ["tenant_id", "supersedes_id"], ["approval_requests.tenant_id", "approval_requests.id"]
        ),
        CheckConstraint(
            "(supersedes_id IS NULL AND editor_id IS NULL AND edit_reason IS NULL) OR "
            "(supersedes_id IS NOT NULL AND editor_id IS NOT NULL AND edit_reason IS NOT NULL)"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "action_id"], ["scheduled_actions.tenant_id", "scheduled_actions.id"]
        ),
        CheckConstraint(
            "(granted_at IS NULL AND expires_at IS NULL AND consumed_at IS NULL) OR "
            "(granted_at IS NOT NULL AND expires_at IS NOT NULL AND "
            "expires_at > granted_at AND expires_at <= granted_at + interval '24 hours')"
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    action_id: Mapped[UUID] = mapped_column(ForeignKey("scheduled_actions.id"))
    supersedes_id: Mapped[UUID | None]
    editor_id: Mapped[UUID | None] = mapped_column(ForeignKey("identity_actors.id"))
    edit_reason: Mapped[str | None]
    action_version: Mapped[int]
    proposer_id: Mapped[UUID] = mapped_column(ForeignKey("identity_actors.id"))
    initiator_id: Mapped[UUID] = mapped_column(ForeignKey("identity_users.id"))
    material_hash: Mapped[str]
    proposal: Mapped[dict[str, object]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(default="pending")
    granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApprovalDecision(Record, Base):
    __tablename__ = "approval_decisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "request_id"], ["approval_requests.tenant_id", "approval_requests.id"]
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    request_id: Mapped[UUID] = mapped_column(ForeignKey("approval_requests.id"))
    approver_id: Mapped[UUID] = mapped_column(ForeignKey("identity_users.id"))
    decision: Mapped[str]
    reason: Mapped[str]
    material_hash: Mapped[str]
    review_binding: Mapped[dict[str, object] | None] = mapped_column(JSON)
