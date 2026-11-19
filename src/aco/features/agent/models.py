"""Run authority and observable evidence, separate from financial/business mutations."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from aco.features.identity.models import Base, Record


class AgentRun(Record, Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "request_id"),
        UniqueConstraint("tenant_id", "source_receipt_id", name="uq_agent_ingress_receipt"),
        ForeignKeyConstraint(
            ["tenant_id", "source_receipt_id"],
            ["email_ingress_receipts.tenant_id", "email_ingress_receipts.id"],
            name="fk_agent_ingress_receipt",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "source_communication_id"],
            ["communications.tenant_id", "communications.id"],
            name="fk_agent_ingress_communication",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "source_activation_id"],
            ["email_ingress_activations.tenant_id", "email_ingress_activations.id"],
            name="fk_agent_ingress_activation",
        ),
        CheckConstraint(
            "(source_receipt_id IS NULL AND source_communication_id IS NULL "
            "AND source_activation_id IS NULL) "
            "OR (source_receipt_id IS NOT NULL AND source_communication_id IS NOT NULL "
            "AND source_activation_id IS NOT NULL "
            "AND authenticity = 'FAKE')",
            name="ck_agent_ingress_fake_scope",
        ),
        ForeignKeyConstraint(["tenant_id", "initiator_id"], ["memberships.tenant_id", "memberships.user_id"]),
        ForeignKeyConstraint(
            ["tenant_id", "worker_actor_id"], ["identity_actors.tenant_id", "identity_actors.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id"],
            ["provider_connections.tenant_id", "provider_connections.id"],
        ),
        CheckConstraint(
            "status IN ('queued','running','awaiting_approval','awaiting_action','completed',"
            "'partially_completed','failed','cancelled')",
            name="ck_agent_run_status",
        ),
        CheckConstraint("authenticity IN ('FAKE','LIVE')"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    request_id: Mapped[UUID]
    source_receipt_id: Mapped[UUID | None]
    source_communication_id: Mapped[UUID | None]
    source_activation_id: Mapped[UUID | None]
    initiator_id: Mapped[UUID]
    membership_version: Mapped[int]
    worker_actor_id: Mapped[UUID]
    actor_version: Mapped[int]
    tenant_generation: Mapped[int]
    provider_connection_id: Mapped[UUID]
    connection_version: Mapped[int]
    correlation_id: Mapped[UUID]
    objective: Mapped[str]
    status: Mapped[str] = mapped_column(default="queued")
    authenticity: Mapped[str] = mapped_column(default="FAKE")
    model_route: Mapped[str] = mapped_column(default="investigation")
    model_id: Mapped[str | None]
    prompt_version: Mapped[str] = mapped_column(default="investigation-v13")
    schema_version: Mapped[str] = mapped_column(default="investigation-v13")
    thread_id: Mapped[str] = mapped_column(unique=True)
    job_id: Mapped[int | None]
    job_version: Mapped[int] = mapped_column(default=1)
    incarnation: Mapped[int] = mapped_column(default=0)
    lease_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    model_state: Mapped[str] = mapped_column(default="not_started")
    result: Mapped[dict[str, object] | None] = mapped_column(JSON)
    error_code: Mapped[str | None]
    last_event_sequence: Mapped[int] = mapped_column(default=0)
    last_step_sequence: Mapped[int] = mapped_column(default=0)
    provider_attempted: Mapped[bool] = mapped_column(default=False)
    requests: Mapped[int] = mapped_column(default=0)
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]


class AgentInvoice(Base):
    __tablename__ = "agent_run_invoices"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "run_id"], ["agent_runs.tenant_id", "agent_runs.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "invoice_id"], ["invoice_snapshots.tenant_id", "invoice_snapshots.id"]
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(primary_key=True)
    run_id: Mapped[UUID] = mapped_column(primary_key=True)
    invoice_id: Mapped[UUID] = mapped_column(primary_key=True)


class AgentStep(Record, Base):
    __tablename__ = "agent_steps"
    __table_args__ = (
        UniqueConstraint("tenant_id", "run_id", "id"),
        UniqueConstraint("run_id", "step_sequence"),
        ForeignKeyConstraint(["tenant_id", "run_id"], ["agent_runs.tenant_id", "agent_runs.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "invoice_id"], ["invoice_snapshots.tenant_id", "invoice_snapshots.id"]
        ),
        CheckConstraint("status IN ('started','completed','failed')"),
    )
    tenant_id: Mapped[UUID] = mapped_column(index=True)
    run_id: Mapped[UUID] = mapped_column(index=True)
    step_sequence: Mapped[int]
    kind: Mapped[str]
    status: Mapped[str]
    tool_name: Mapped[str | None]
    description: Mapped[str]
    invoice_id: Mapped[UUID | None]
    input: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    output: Mapped[dict[str, object] | None] = mapped_column(JSON)
    error_code: Mapped[str | None]


class AgentEvent(Record, Base):
    __tablename__ = "agent_events"
    __table_args__ = (
        UniqueConstraint("run_id", "event_sequence"),
        ForeignKeyConstraint(["tenant_id", "run_id"], ["agent_runs.tenant_id", "agent_runs.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "step_id"],
            ["agent_steps.tenant_id", "agent_steps.run_id", "agent_steps.id"],
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(index=True)
    run_id: Mapped[UUID] = mapped_column(index=True)
    event_sequence: Mapped[int]
    kind: Mapped[str]
    step_id: Mapped[UUID | None]
    description: Mapped[str]


class AgentUsage(Base):
    __tablename__ = "agent_usage"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "step_id"],
            ["agent_steps.tenant_id", "agent_steps.run_id", "agent_steps.id"],
        ),
        ForeignKeyConstraint(["tenant_id", "attempt_id"], ["usage_attempts.tenant_id", "usage_attempts.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "event_id"], ["provider_usage_events.tenant_id", "provider_usage_events.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "reservation_id"], ["budget_reservations.tenant_id", "budget_reservations.id"]
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(primary_key=True)
    run_id: Mapped[UUID] = mapped_column(primary_key=True)
    step_id: Mapped[UUID]
    attempt_id: Mapped[UUID] = mapped_column(primary_key=True)
    event_id: Mapped[UUID | None]

    reservation_id: Mapped[UUID | None]
    observation: Mapped[dict[str, object] | None] = mapped_column(JSON)
