"""Canonical policy/version foundation. T08 adds editing, validation and activation."""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from aco.features.identity.models import Base, Record


class CollectionPolicy(Record, Base):
    __tablename__ = "collection_policies"
    __table_args__ = (UniqueConstraint("tenant_id", "id"), UniqueConstraint("tenant_id", "name"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str]
    active: Mapped[bool] = mapped_column(default=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CollectionPolicyVersion(Record, Base):
    __tablename__ = "collection_policy_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "policy_id"], ["collection_policies.tenant_id", "collection_policies.id"]
        ),
        UniqueConstraint("tenant_id", "policy_id", "version"),
        UniqueConstraint("tenant_id", "id"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    policy_id: Mapped[UUID]
    state: Mapped[str] = mapped_column(default="DRAFT")
    definition: Mapped[dict[str, object]] = mapped_column(JSON)
    synthetic: Mapped[bool] = mapped_column(default=True)


class PolicyEvent(Record, Base):
    """Actual policy trigger, linked to the single canonical audit ledger."""

    __tablename__ = "policy_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "audit_event_id"], ["identity_audit_events.tenant_id", "identity_audit_events.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "policy_version_id"],
            ["collection_policy_versions.tenant_id", "collection_policy_versions.id"],
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    policy_version_id: Mapped[UUID]
    audit_event_id: Mapped[UUID] = mapped_column(ForeignKey("identity_audit_events.id"))
    kind: Mapped[str]


class PolicyActivation(Record, Base):
    __tablename__ = "policy_activations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "id", "event_id"),
        UniqueConstraint("tenant_id", "id", "policy_version_id"),
        ForeignKeyConstraint(
            ["tenant_id", "worker_actor_id"], ["identity_actors.tenant_id", "identity_actors.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "enabling_human_id"], ["memberships.tenant_id", "memberships.user_id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "policy_version_id"],
            ["collection_policy_versions.tenant_id", "collection_policy_versions.id"],
        ),
        ForeignKeyConstraint(["tenant_id", "event_id"], ["policy_events.tenant_id", "policy_events.id"]),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    policy_version_id: Mapped[UUID]
    event_id: Mapped[UUID]
    enabling_human_id: Mapped[UUID] = mapped_column(ForeignKey("identity_users.id"))
    membership_version: Mapped[int]
    worker_actor_id: Mapped[UUID] = mapped_column(ForeignKey("identity_actors.id"))
    actor_version: Mapped[int]
    tenant_generation: Mapped[int]
    outbound_paused: Mapped[bool]
    native_reminders_disabled: Mapped[bool]
    configuration_reviewed: Mapped[bool]


class RecipientControl(Record, Base):
    __tablename__ = "policy_recipient_controls"
    __table_args__ = (
        UniqueConstraint("tenant_id", "contact_id"),
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id", "contact_id"],
            [
                "customer_contacts.tenant_id",
                "customer_contacts.provider_connection_id",
                "customer_contacts.id",
            ],
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    provider_connection_id: Mapped[UUID]
    contact_id: Mapped[UUID]
    reviewer_id: Mapped[UUID] = mapped_column(ForeignKey("identity_users.id"))
    reviewed_destination: Mapped[str | None]
    reviewed_contact_revision: Mapped[str]
    timezone: Mapped[str | None]
    timezone_verified: Mapped[bool] = mapped_column(default=False)
    owner_confirmed_tenant_timezone: Mapped[bool] = mapped_column(default=False)
    confirmed_tenant_timezone: Mapped[str | None]
    destination_verified: Mapped[bool] = mapped_column(default=False)
    allowed_channels: Mapped[list[str]] = mapped_column(JSON)
    opt_out: Mapped[bool] = mapped_column(default=False)
    evidence: Mapped[str]


class PolicyOccurrence(Record, Base):
    __tablename__ = "policy_occurrences"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "operation_key"),
        ForeignKeyConstraint(
            ["tenant_id", "invoice_id"], ["invoice_snapshots.tenant_id", "invoice_snapshots.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "policy_id"], ["collection_policies.tenant_id", "collection_policies.id"]
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    invoice_id: Mapped[UUID]
    policy_id: Mapped[UUID]
    rule_id: Mapped[UUID]
    due_date_anchor: Mapped[date] = mapped_column(Date)
    operation_key: Mapped[str]
    state: Mapped[str] = mapped_column(default="available")
    reconsider_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reason: Mapped[str | None]


class CollectionHold(Record, Base):
    __tablename__ = "collection_holds"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "invoice_id"], ["invoice_snapshots.tenant_id", "invoice_snapshots.id"]
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    invoice_id: Mapped[UUID]
    reason: Mapped[str]
    evidence: Mapped[str]
    creator_id: Mapped[UUID] = mapped_column(ForeignKey("identity_actors.id"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    release_evidence: Mapped[str | None]


class ContactReservation(Record, Base):
    __tablename__ = "contact_reservations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_contact_reservations_tenant_id"),
        UniqueConstraint("tenant_id", "operation_id"),
        ForeignKeyConstraint(
            ["tenant_id", "invoice_id", "customer_id"],
            ["invoice_snapshots.tenant_id", "invoice_snapshots.id", "invoice_snapshots.customer_id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "invoice_id"], ["invoice_snapshots.tenant_id", "invoice_snapshots.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "occurrence_id"], ["policy_occurrences.tenant_id", "policy_occurrences.id"]
        ),
        CheckConstraint(
            "status IN ('reserved','inflight','accepted','connected','unknown_outcome','released')"
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    invoice_id: Mapped[UUID]
    customer_id: Mapped[UUID] = mapped_column(ForeignKey("customers.id"), index=True)
    destination: Mapped[str] = mapped_column(index=True)
    channel: Mapped[str]
    operation_id: Mapped[UUID]
    occurrence_id: Mapped[UUID | None]
    action_id: Mapped[UUID | None] = mapped_column(ForeignKey("scheduled_actions.id"))
    status: Mapped[str] = mapped_column(default="reserved")
    reserved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidence: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)


class DispatchRead(Record, Base):
    __tablename__ = "dispatch_reads"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "invoice_id", "observation_id"],
            [
                "accounting_observations.tenant_id",
                "accounting_observations.invoice_id",
                "accounting_observations.id",
            ],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "invoice_id"], ["invoice_snapshots.tenant_id", "invoice_snapshots.id"]
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    invoice_id: Mapped[UUID]
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("identity_actors.id"))
    action_id: Mapped[UUID | None] = mapped_column(ForeignKey("scheduled_actions.id"))
    action_version: Mapped[int | None]
    lease_token: Mapped[UUID | None]
    attempt_id: Mapped[UUID]
    incarnation: Mapped[UUID]
    observation_id: Mapped[UUID | None] = mapped_column(ForeignKey("accounting_observations.id"))
    error: Mapped[str | None]
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DispatchTicketRecord(Record, Base):
    __tablename__ = "dispatch_tickets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "action_id"], ["scheduled_actions.tenant_id", "scheduled_actions.id"]
        ),
        ForeignKeyConstraint(["tenant_id", "read_id"], ["dispatch_reads.tenant_id", "dispatch_reads.id"]),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    action_id: Mapped[UUID]
    attempt_id: Mapped[UUID]
    read_id: Mapped[UUID]
    incarnation: Mapped[UUID]
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bindings: Mapped[dict[str, object]] = mapped_column(JSON)
