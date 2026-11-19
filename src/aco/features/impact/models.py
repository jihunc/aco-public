"""Frozen cohort populations and append-only result versions, scoped to real source identities."""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from aco.features.identity.models import Base, Record


class CohortPair(Record, Base):
    __tablename__ = "impact_cohort_pairs"
    __table_args__ = (
        Index(
            "one_illustrative_cohort_per_tenant",
            "tenant_id",
            unique=True,
            postgresql_where=text("purpose = 'ILLUSTRATION'"),
        ),
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "request_id"),
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id", "manifest_id"],
            [
                "demo_source_records.tenant_id",
                "demo_source_records.provider_connection_id",
                "demo_source_records.id",
            ],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id"],
            ["provider_connections.tenant_id", "provider_connections.id"],
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    provider_connection_id: Mapped[UUID]
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("identity_actors.id"))
    request_id: Mapped[UUID]
    command: Mapped[dict[str, object]] = mapped_column(JSON)
    manifest_id: Mapped[UUID | None]
    purpose: Mapped[str] = mapped_column(default="ILLUSTRATION", server_default="ILLUSTRATION")


class InvoiceCohort(Record, Base):
    __tablename__ = "invoice_cohorts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "pair_id", "side", "version"),
        ForeignKeyConstraint(
            ["tenant_id", "prior_cohort_id"], ["invoice_cohorts.tenant_id", "invoice_cohorts.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "pair_id"], ["impact_cohort_pairs.tenant_id", "impact_cohort_pairs.id"]
        ),
        CheckConstraint("side IN ('BASELINE_SYNTHETIC','AFTER_SYNTHETIC','OBSERVED')"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    pair_id: Mapped[UUID]
    side: Mapped[str]
    prior_cohort_id: Mapped[UUID | None]
    definition: Mapped[dict[str, object]] = mapped_column(JSON)


class InvoiceCohortMembership(Record, Base):
    __tablename__ = "invoice_cohort_memberships"
    __table_args__ = (
        UniqueConstraint("tenant_id", "cohort_id", "invoice_id"),
        ForeignKeyConstraint(["tenant_id", "cohort_id"], ["invoice_cohorts.tenant_id", "invoice_cohorts.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "invoice_id"], ["invoice_snapshots.tenant_id", "invoice_snapshots.id"]
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    cohort_id: Mapped[UUID]
    invoice_id: Mapped[UUID]
    basis: Mapped[dict[str, object]] = mapped_column(JSON)


class CohortResult(Record, Base):
    __tablename__ = "impact_cohort_results"
    __table_args__ = (
        UniqueConstraint("tenant_id", "cohort_id", "id"),
        UniqueConstraint("tenant_id", "cohort_id", "version"),
        ForeignKeyConstraint(["tenant_id", "cohort_id"], ["invoice_cohorts.tenant_id", "invoice_cohorts.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "cohort_id", "prior_result_id"],
            [
                "impact_cohort_results.tenant_id",
                "impact_cohort_results.cohort_id",
                "impact_cohort_results.id",
            ],
        ),
        CheckConstraint("version > 0"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    cohort_id: Mapped[UUID]
    prior_result_id: Mapped[UUID | None]
    evidence: Mapped[dict[str, object]] = mapped_column(JSON)
    result: Mapped[dict[str, object]] = mapped_column(JSON)


class CohortRefresh(Record, Base):
    __tablename__ = "impact_cohort_refreshes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "request_id"),
        ForeignKeyConstraint(
            ["tenant_id", "pair_id"], ["impact_cohort_pairs.tenant_id", "impact_cohort_pairs.id"]
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    pair_id: Mapped[UUID]
    request_id: Mapped[UUID]
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("identity_actors.id"))
    command: Mapped[dict[str, object]] = mapped_column(JSON)


class HumanHandlingEntry(Record, Base):
    """Append-only self-reported time for an actual substantive case-history event."""

    __tablename__ = "human_handling_entries"
    __table_args__ = (
        UniqueConstraint("tenant_id", "request_id"),
        UniqueConstraint("tenant_id", "history_id", "id"),
        UniqueConstraint("tenant_id", "history_id", "version"),
        ForeignKeyConstraint(["tenant_id", "case_id"], ["collection_cases.tenant_id", "collection_cases.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "history_id", "supersedes_id"],
            [
                "human_handling_entries.tenant_id",
                "human_handling_entries.history_id",
                "human_handling_entries.id",
            ],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "audit_id"], ["identity_audit_events.tenant_id", "identity_audit_events.id"]
        ),
        CheckConstraint("version > 0 AND minutes >= 0 AND minutes <= 1440"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    case_id: Mapped[UUID]
    history_id: Mapped[UUID] = mapped_column(ForeignKey("case_history.id"))
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("identity_actors.id"))
    audit_id: Mapped[UUID]
    request_id: Mapped[UUID]
    command: Mapped[dict[str, object]] = mapped_column(JSON)
    minutes: Mapped[Decimal] = mapped_column(Numeric(8, 2))
    evidence: Mapped[str]
    supersedes_id: Mapped[UUID | None]


class EconomicsAssumptions(Record, Base):
    __tablename__ = "economics_assumptions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "request_id"),
        UniqueConstraint("tenant_id", "version"),
        ForeignKeyConstraint(
            ["tenant_id", "previous_version_id"],
            ["economics_assumptions.tenant_id", "economics_assumptions.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "audit_id"], ["identity_audit_events.tenant_id", "identity_audit_events.id"]
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    request_id: Mapped[UUID]
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("identity_actors.id"))
    audit_id: Mapped[UUID]
    previous_version_id: Mapped[UUID | None]
    command: Mapped[dict[str, object]] = mapped_column(JSON)
    reviewed_hash: Mapped[str]


class EconomicsApproval(Record, Base):
    __tablename__ = "economics_approvals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "request_id"),
        UniqueConstraint("tenant_id", "assumptions_id"),
        UniqueConstraint("tenant_id", "version"),
        ForeignKeyConstraint(
            ["tenant_id", "assumptions_id"], ["economics_assumptions.tenant_id", "economics_assumptions.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "previous_approval_id"], ["economics_approvals.tenant_id", "economics_approvals.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "audit_id"], ["identity_audit_events.tenant_id", "identity_audit_events.id"]
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    request_id: Mapped[UUID]
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("identity_actors.id"))
    audit_id: Mapped[UUID]
    assumptions_id: Mapped[UUID]
    previous_approval_id: Mapped[UUID | None]
    command: Mapped[dict[str, object]] = mapped_column(JSON)


class CostAllocationVersion(Record, Base):
    __tablename__ = "cost_allocation_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "event_id", "id"),
        UniqueConstraint("tenant_id", "request_id"),
        UniqueConstraint("tenant_id", "event_id", "version"),
        ForeignKeyConstraint(
            ["tenant_id", "event_id"], ["provider_usage_events.tenant_id", "provider_usage_events.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "event_id", "previous_version_id"],
            [
                "cost_allocation_versions.tenant_id",
                "cost_allocation_versions.event_id",
                "cost_allocation_versions.id",
            ],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "audit_id"], ["identity_audit_events.tenant_id", "identity_audit_events.id"]
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    request_id: Mapped[UUID]
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("identity_actors.id"))
    audit_id: Mapped[UUID]
    event_id: Mapped[UUID]
    revision_id: Mapped[UUID] = mapped_column(ForeignKey("usage_revisions.id"))
    previous_version_id: Mapped[UUID | None]
    basis: Mapped[str]
    command: Mapped[dict[str, object]] = mapped_column(JSON)
    leaves: Mapped[list[dict[str, object]]] = mapped_column(JSON)
    source_ids: Mapped[list[str]] = mapped_column(JSON)
