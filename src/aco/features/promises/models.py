"""Versioned commitments and immutable genuine promise events; never payment facts."""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from aco.features.cases.models import scoped
from aco.features.identity.models import Base, Record


class PaymentPromise(Record, Base):
    __tablename__ = "payment_promises"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "capture_key"),
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id", "customer_id"],
            ["customers.tenant_id", "customers.provider_connection_id", "customers.id"],
        ),
        scoped("source_communication_id", "communications"),
        scoped("source_voice_evidence_id", "voice_business_evidence"),
        scoped("confirmation_voice_evidence_id", "voice_business_evidence"),
        CheckConstraint("(source_communication_id IS NOT NULL) <> (source_voice_evidence_id IS NOT NULL)"),
        CheckConstraint("confirmation_communication_id IS NULL OR confirmation_voice_evidence_id IS NULL"),
        scoped("confirmation_communication_id", "communications"),
        scoped("replaces_promise_id", "payment_promises"),
        scoped("superseded_by_id", "payment_promises"),
        CheckConstraint(
            "status IN ('CAPTURED','NEEDS_CONFIRMATION','ACTIVE','FULFILLED',"
            "'MISSED','SUPERSEDED','CANCELLED')"
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    customer_id: Mapped[UUID]
    provider_connection_id: Mapped[UUID]
    capture_key: Mapped[str]
    status: Mapped[str] = mapped_column(default="NEEDS_CONFIRMATION")
    source_communication_id: Mapped[UUID | None]
    source_voice_evidence_id: Mapped[UUID | None]
    source_text: Mapped[str]
    source_offset: Mapped[int]
    source_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_state: Mapped[str]
    synthetic: Mapped[bool]
    amount: Mapped[dict[str, object] | None] = mapped_column(JSON)
    due_date: Mapped[date | None] = mapped_column(Date)
    timezone: Mapped[str | None]
    cutoff_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmation_communication_id: Mapped[UUID | None]
    confirmation_voice_evidence_id: Mapped[UUID | None]
    confirmation_reasons: Mapped[list[str]] = mapped_column(JSON)
    candidate_invoice_ids: Mapped[list[str]] = mapped_column(JSON)
    replaces_promise_id: Mapped[UUID | None]
    superseded_by_id: Mapped[UUID | None]
    review_task_id: Mapped[UUID | None] = mapped_column(ForeignKey("collection_tasks.id"))
    command: Mapped[dict[str, object]] = mapped_column(JSON)


class PromiseInvoiceLink(Record, Base):
    __tablename__ = "promise_invoice_links"
    __table_args__ = (
        UniqueConstraint("tenant_id", "promise_id", "invoice_id"),
        scoped("promise_id", "payment_promises"),
        scoped("invoice_id", "invoice_snapshots"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    promise_id: Mapped[UUID]
    invoice_id: Mapped[UUID]
    amount: Mapped[dict[str, object] | None] = mapped_column(JSON)
    hold_id: Mapped[UUID | None] = mapped_column(ForeignKey("collection_holds.id"))
    verification_hold_id: Mapped[UUID | None] = mapped_column(ForeignKey("collection_holds.id"))


class PromiseInstallment(Record, Base):
    __tablename__ = "promise_installments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "promise_id", "sequence"),
        scoped("promise_id", "payment_promises"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    promise_id: Mapped[UUID]
    sequence: Mapped[int]
    due_date: Mapped[date] = mapped_column(Date)
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    amount: Mapped[dict[str, object]] = mapped_column(JSON)
    allocations: Mapped[list[dict[str, object]]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(default="PENDING_CONFIRMATION")


class PromiseEvent(Record, Base):
    __tablename__ = "promise_events"
    __table_args__ = (UniqueConstraint("tenant_id", "id"), scoped("promise_id", "payment_promises"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    promise_id: Mapped[UUID]
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("identity_actors.id"))
    audit_event_id: Mapped[UUID] = mapped_column(ForeignKey("identity_audit_events.id"))
    event: Mapped[str]
    details: Mapped[dict[str, object]] = mapped_column(JSON)


class CustomerScopeReview(Record, Base):
    __tablename__ = "customer_scope_reviews"
    __table_args__ = (
        UniqueConstraint("tenant_id", "promise_id"),
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id", "customer_id"],
            ["customers.tenant_id", "customers.provider_connection_id", "customers.id"],
        ),
        scoped("promise_id", "payment_promises"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    provider_connection_id: Mapped[UUID]
    customer_id: Mapped[UUID] = mapped_column(index=True)
    promise_id: Mapped[UUID]
    reason: Mapped[str] = mapped_column(default="scope_review")
    hold_ids: Mapped[list[str]] = mapped_column(JSON)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    release_evidence: Mapped[str | None]


class PromiseAllocationClaim(Record, Base):
    """Source cash belongs to one promise; immutable portions prevent installment double counting."""

    __tablename__ = "promise_allocation_claims"
    __table_args__ = (
        UniqueConstraint("tenant_id", "allocation_id", "installment_id"),
        CheckConstraint("claimed_minor > 0"),
        scoped("promise_id", "payment_promises"),
        scoped("installment_id", "promise_installments"),
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id", "allocation_id"],
            [
                "verified_payment_allocations.tenant_id",
                "verified_payment_allocations.provider_connection_id",
                "verified_payment_allocations.id",
            ],
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    provider_connection_id: Mapped[UUID]
    allocation_id: Mapped[UUID]
    claimed_minor: Mapped[int] = mapped_column(BigInteger)
    promise_id: Mapped[UUID]
    installment_id: Mapped[UUID]


class PromiseVerification(Record, Base):
    """Append-only observed performance, separate from financial truth and retained lifecycle episodes."""

    __tablename__ = "promise_verifications"
    __table_args__ = (
        UniqueConstraint("tenant_id", "installment_id", "evidence_key"),
        scoped("promise_id", "payment_promises"),
        scoped("installment_id", "promise_installments"),
        scoped("event_id", "promise_events"),
        CheckConstraint("verification_status IN ('VERIFIED','PENDING_REVIEW')"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    promise_id: Mapped[UUID]
    installment_id: Mapped[UUID]
    event_id: Mapped[UUID]
    evidence_key: Mapped[str]
    verification_status: Mapped[str]
    prior_status: Mapped[str]
    resulting_status: Mapped[str]
    timely_minor: Mapped[int | None] = mapped_column(BigInteger)
    late_minor: Mapped[int | None] = mapped_column(BigInteger)
    outside_minor: Mapped[int | None] = mapped_column(BigInteger)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict[str, object]] = mapped_column(JSON)
