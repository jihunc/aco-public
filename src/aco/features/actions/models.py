"""Durable domain operations; Procrastinate owns queue scheduling and worker heartbeats."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from aco.features.identity.models import Base, Record


class ScheduledAction(Record, Base):
    __tablename__ = "scheduled_actions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "voice_session_id", "id"),
        UniqueConstraint("tenant_id", "operation_key"),
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id", "invoice_id"],
            [
                "invoice_snapshots.tenant_id",
                "invoice_snapshots.provider_connection_id",
                "invoice_snapshots.id",
            ],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "policy_event_id"], ["policy_events.tenant_id", "policy_events.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "policy_activation_id"], ["policy_activations.tenant_id", "policy_activations.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "occurrence_id"], ["policy_occurrences.tenant_id", "policy_occurrences.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "policy_version_id"],
            ["collection_policy_versions.tenant_id", "collection_policy_versions.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "policy_activation_id", "policy_event_id"],
            ["policy_activations.tenant_id", "policy_activations.id", "policy_activations.event_id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "policy_activation_id", "policy_version_id"],
            ["policy_activations.tenant_id", "policy_activations.id", "policy_activations.policy_version_id"],
        ),
        CheckConstraint(
            "(((((kind='policy_due' AND agent_run_id IS NULL AND agent_step_id IS NULL "
            "AND source_event_id IS NULL "
            "AND policy_event_id IS NOT NULL "
            "AND policy_activation_id IS NOT NULL AND occurrence_id IS NOT NULL) OR "
            "(kind IN ('accounting_recheck','internal_task','approval_request') "
            "AND source_event_id IS NOT NULL AND agent_run_id IS NULL AND agent_step_id IS NULL "
            "AND policy_event_id IS NULL AND policy_activation_id IS NULL AND occurrence_id IS NULL) OR "
            "(kind='agent_internal' AND source_event_id IS NULL AND policy_event_id IS NULL "
            "AND policy_activation_id IS NULL AND occurrence_id IS NULL AND policy_version_id IS NULL "
            "AND agent_run_id IS NOT NULL AND agent_step_id IS NOT NULL)) "
            "AND promise_event_id IS NULL AND promise_installment_id IS NULL) OR "
            "(kind='promise_check' AND promise_event_id IS NOT NULL AND promise_installment_id IS NOT NULL "
            "AND source_event_id IS NULL AND policy_event_id IS NULL AND policy_activation_id IS NULL "
            "AND occurrence_id IS NULL AND policy_version_id IS NULL AND agent_run_id IS NULL "
            "AND agent_step_id IS NULL AND reservation_id IS NULL)) "
            "AND voice_session_id IS NULL AND voice_invocation_id IS NULL) OR "
            "(kind IN ('voice_session','voice_internal') AND voice_session_id IS NOT NULL "
            "AND ((kind='voice_session' AND voice_invocation_id IS NULL) OR "
            "(kind='voice_internal' AND voice_invocation_id IS NOT NULL)) "
            "AND source_event_id IS NULL AND policy_event_id IS NULL AND policy_activation_id IS NULL "
            "AND occurrence_id IS NULL AND policy_version_id IS NULL AND agent_run_id IS NULL "
            "AND agent_step_id IS NULL AND promise_event_id IS NULL AND promise_installment_id IS NULL "
            "AND reservation_id IS NULL)"
            " OR (kind='agent_email'"
            " AND agent_run_id IS NOT NULL"
            " AND agent_step_id IS NOT NULL"
            " AND policy_version_id IS NOT NULL"
            " AND source_event_id IS NULL"
            " AND policy_event_id IS NULL"
            " AND policy_activation_id IS NULL"
            " AND occurrence_id IS NULL"
            " AND promise_event_id IS NULL"
            " AND promise_installment_id IS NULL"
            " AND voice_session_id IS NULL"
            " AND voice_invocation_id IS NULL"
            " AND reservation_id IS NULL)",
            name="ck_action_trigger",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "agent_run_id", "agent_step_id"],
            ["agent_steps.tenant_id", "agent_steps.run_id", "agent_steps.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "promise_event_id"], ["promise_events.tenant_id", "promise_events.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "promise_installment_id"],
            ["promise_installments.tenant_id", "promise_installments.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "voice_session_id"], ["voice_sessions.tenant_id", "voice_sessions.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "voice_session_id", "voice_invocation_id"],
            ["voice_invocations.tenant_id", "voice_invocations.session_id", "voice_invocations.id"],
        ),
        CheckConstraint("attempts BETWEEN 0 AND 3"),
        CheckConstraint(
            "status IN ('proposed','validated','awaiting_approval','queued','executing',"
            "'succeeded','failed','unknown_outcome','cancelled','dead_letter')"
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    provider_connection_id: Mapped[UUID]
    invoice_id: Mapped[UUID]
    source_revision: Mapped[str]
    source_event_id: Mapped[UUID | None] = mapped_column(ForeignKey("accounting_events.id"))
    policy_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("collection_policy_versions.id"))
    policy_event_id: Mapped[UUID | None]
    policy_activation_id: Mapped[UUID | None]
    occurrence_id: Mapped[UUID | None]
    policy_generation: Mapped[int]
    initiator_id: Mapped[UUID] = mapped_column(ForeignKey("identity_users.id"))
    membership_version: Mapped[int]
    tenant_version: Mapped[int]
    outbound_paused: Mapped[bool]
    automation_enabled: Mapped[bool]
    worker_actor_id: Mapped[UUID] = mapped_column(ForeignKey("identity_actors.id"))
    actor_version: Mapped[int]
    connection_version: Mapped[int]
    correlation_id: Mapped[UUID]
    kind: Mapped[str]
    operation_key: Mapped[str]
    command: Mapped[dict[str, object]] = mapped_column(JSON)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(default="queued")
    attempts: Mapped[int] = mapped_column(default=0)
    automatic_resend: Mapped[bool] = mapped_column(default=False)
    last_error: Mapped[str | None]
    result: Mapped[dict[str, object] | None] = mapped_column(JSON)
    lease_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    job_id: Mapped[int | None]
    agent_run_id: Mapped[UUID | None]
    agent_step_id: Mapped[UUID | None]
    voice_session_id: Mapped[UUID | None]
    voice_invocation_id: Mapped[UUID | None]
    promise_event_id: Mapped[UUID | None]
    promise_installment_id: Mapped[UUID | None]
    reservation_id: Mapped[UUID | None] = mapped_column(ForeignKey("budget_reservations.id"))


class ActionExecution(Record, Base):
    __tablename__ = "action_executions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "action_id"], ["scheduled_actions.tenant_id", "scheduled_actions.id"]
        ),
        UniqueConstraint("action_id", "attempt"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    action_id: Mapped[UUID]
    attempt: Mapped[int]
    lease_token: Mapped[UUID] = mapped_column(unique=True)
    status: Mapped[str] = mapped_column(default="executing")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_delay_seconds: Mapped[int | None]
    provider_operation_id: Mapped[str | None]
    error: Mapped[dict[str, object] | None] = mapped_column(JSON)
    result: Mapped[dict[str, object] | None] = mapped_column(JSON)


class OutboxEvent(Record, Base):
    __tablename__ = "action_outbox"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "action_id"], ["scheduled_actions.tenant_id", "scheduled_actions.id"]
        ),
        UniqueConstraint("action_id", "action_version"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    action_id: Mapped[UUID]
    action_version: Mapped[int]
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    job_id: Mapped[int | None]
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ActionSource(Record, Base):
    """Every original source event survives deduplication by actual financial revision."""

    __tablename__ = "action_sources"
    __table_args__ = (
        UniqueConstraint("action_id", "source_event_id"),
        ForeignKeyConstraint(
            ["tenant_id", "action_id"], ["scheduled_actions.tenant_id", "scheduled_actions.id"]
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    action_id: Mapped[UUID] = mapped_column(ForeignKey("scheduled_actions.id"))
    source_event_id: Mapped[UUID] = mapped_column(ForeignKey("accounting_events.id"))
