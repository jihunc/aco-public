"""Cases, N:M invoice dispositions and immutable source/history references."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from aco.features.identity.models import Base, Record


def scoped(column: str, table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(["tenant_id", column], [f"{table}.tenant_id", f"{table}.id"])


class CollectionCase(Record, Base):
    __tablename__ = "collection_cases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "issue_identity"),
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id", "customer_id"],
            ["customers.tenant_id", "customers.provider_connection_id", "customers.id"],
        ),
        CheckConstraint(
            "status IN ('OPEN','INVESTIGATING','WAITING_CUSTOMER','WAITING_INTERNAL',"
            "'AWAITING_APPROVAL','RESOLVED','CLOSED')"
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    customer_id: Mapped[UUID]
    provider_connection_id: Mapped[UUID]
    issue_identity: Mapped[str]
    issue_type: Mapped[str]
    claim_key: Mapped[str]
    substance: Mapped[str]
    status: Mapped[str] = mapped_column(default="OPEN")
    review_required: Mapped[bool] = mapped_column(default=False)
    task_id: Mapped[UUID] = mapped_column(ForeignKey("collection_tasks.id"))
    synthetic: Mapped[bool]
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closure_reason: Mapped[str | None]


class CaseEvidence(Record, Base):
    __tablename__ = "case_evidence"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "case_id", "evidence_key"),
        scoped("case_id", "collection_cases"),
        scoped("communication_id", "communications"),
        scoped("voice_evidence_id", "voice_business_evidence"),
        CheckConstraint("(communication_id IS NOT NULL) <> (voice_evidence_id IS NOT NULL)"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    case_id: Mapped[UUID]
    communication_id: Mapped[UUID | None]
    voice_evidence_id: Mapped[UUID | None]
    kind: Mapped[str]
    evidence_key: Mapped[str]
    excerpt: Mapped[str]
    offset: Mapped[int]
    synthetic: Mapped[bool]
    source_state: Mapped[str]


class CaseInvoiceLink(Record, Base):
    __tablename__ = "case_invoice_links"
    __table_args__ = (
        UniqueConstraint("tenant_id", "case_id", "invoice_id"),
        scoped("case_id", "collection_cases"),
        scoped("invoice_id", "invoice_snapshots"),
        scoped("resolution_evidence_id", "case_evidence"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    case_id: Mapped[UUID]
    invoice_id: Mapped[UUID]
    issue_state: Mapped[str] = mapped_column(default="OPEN")
    impact: Mapped[str]
    hold_id: Mapped[UUID | None] = mapped_column(ForeignKey("collection_holds.id"))
    resolution_action: Mapped[str | None]
    resolution_evidence_id: Mapped[UUID | None]
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CaseHistory(Record, Base):
    __tablename__ = "case_history"
    __table_args__ = (scoped("case_id", "collection_cases"),)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    case_id: Mapped[UUID]
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("identity_actors.id"))
    event: Mapped[str]
    substantive_human: Mapped[bool]
    details: Mapped[dict[str, object]] = mapped_column(JSON)
