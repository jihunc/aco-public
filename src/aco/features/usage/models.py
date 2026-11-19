"""One canonical charge identity; revisions and reconciliation retain original estimates."""

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


class ProviderUsageEvent(Record, Base):
    __tablename__ = "provider_usage_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id"],
            ["provider_connections.tenant_id", "provider_connections.id"],
        ),
        UniqueConstraint("scope_key", "provider_connection_id", "operation_key", "component"),
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(
            "(scope_kind = 'TENANT' AND tenant_id IS NOT NULL) OR (scope_kind = "
            "'GLOBAL_IDENTITY' AND tenant_id IS NULL)"
        ),
    )
    tenant_id: Mapped[UUID | None] = mapped_column(ForeignKey("tenants.id"), index=True)
    scope_kind: Mapped[str]
    scope_key: Mapped[str]
    provider_connection_id: Mapped[UUID]
    operation_key: Mapped[str]
    component: Mapped[str]
    operation: Mapped[str]
    provider_kind: Mapped[str]
    provider: Mapped[str]
    correlation_id: Mapped[UUID]
    native_operation_id: Mapped[str | None]
    native_subject: Mapped[str | None]
    synthetic: Mapped[bool]
    reconstructible: Mapped[bool] = mapped_column(default=False)


class UsageRevision(Record, Base):
    __tablename__ = "usage_revisions"
    __table_args__ = (
        UniqueConstraint("event_id", "reconciliation_key"),
        CheckConstraint(
            "(cost_quality = 'UNKNOWN' AND cost_amount IS NULL) OR "
            "(cost_quality IN ('PROVIDER_REPORTED','RATE_ESTIMATE') AND cost_amount IS NOT NULL)",
            name="quality_amount",
        ),
        CheckConstraint(
            "cost_amount >= 0 AND cost_amount < 100000000000000000000000000", name="finite_cost_amount"
        ),
        CheckConstraint(
            "(cost_quality = 'UNKNOWN' AND cost_amount IS NULL) OR (cost_quality IN "
            "('PROVIDER_REPORTED','RATE_ESTIMATE') AND cost_amount >= 0 AND currency IS "
            "NOT NULL)"
        ),
    )
    event_id: Mapped[UUID] = mapped_column(ForeignKey("provider_usage_events.id"), index=True)
    reconciliation_key: Mapped[str]
    cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(38, 12))
    currency: Mapped[str | None]
    cost_quality: Mapped[str]
    outcome: Mapped[str]
    payload: Mapped[dict[str, object]] = mapped_column(JSON)


class UsageSource(Record, Base):
    __tablename__ = "usage_sources"
    __table_args__ = (UniqueConstraint("source_kind", "source_id", "component"),)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("provider_usage_events.id"))
    source_kind: Mapped[str]
    source_id: Mapped[UUID]
    component: Mapped[str]


class RateSnapshot(Record, Base):
    __tablename__ = "usage_rates"
    __table_args__ = (
        CheckConstraint("amount >= 0 AND amount < 100000000000000000000000000", name="finite_amount"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    provider: Mapped[str]
    currency: Mapped[str]
    unit: Mapped[str]
    amount: Mapped[Decimal] = mapped_column(Numeric(38, 12))
    price_source_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    synthetic: Mapped[bool]


class BudgetScope(Record, Base):
    __tablename__ = "budget_scopes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id"],
            ["provider_connections.tenant_id", "provider_connections.id"],
        ),
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint("dimension IN ('tenant','provider','run','call','demo_session')"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    provider_connection_id: Mapped[UUID | None]
    dimension: Mapped[str]
    synthetic: Mapped[bool]
    creator_id: Mapped[UUID] = mapped_column(ForeignKey("identity_actors.id"))


class BudgetLimit(Record, Base):
    __tablename__ = "budget_limits"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "scope_id"], ["budget_scopes.tenant_id", "budget_scopes.id"]),
        UniqueConstraint("scope_id", "currency", "state"),
        CheckConstraint("state IN ('DRAFT','SYNTHETIC_TEST_ONLY','BOUNDED_LIVE_TEST')"),
        CheckConstraint("amount >= 0 AND requests >= 0"),
        CheckConstraint("amount >= 0 AND amount < 100000000000000000000000000", name="finite_amount"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    scope_id: Mapped[UUID]
    currency: Mapped[str]
    amount: Mapped[Decimal] = mapped_column(Numeric(38, 12))
    requests: Mapped[int]
    unit_caps: Mapped[dict[str, str | int]] = mapped_column(JSON, default=dict)
    state: Mapped[str]


class BudgetReservation(Record, Base):
    __tablename__ = "budget_reservations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id"],
            ["provider_connections.tenant_id", "provider_connections.id"],
        ),
        UniqueConstraint("tenant_id", "provider_connection_id", "operation_key"),
        *(
            CheckConstraint(f"{name} >= 0 AND {name} < 100000000000000000000000000", name=f"finite_{name}")
            for name in ("original_amount", "retained_amount", "released_amount", "spent_amount")
        ),
        UniqueConstraint("tenant_id", "id"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    provider_connection_id: Mapped[UUID]
    operation_key: Mapped[str]
    currency: Mapped[str]
    status: Mapped[str]
    original_amount: Mapped[Decimal | None] = mapped_column(Numeric(38, 12))
    retained_amount: Mapped[Decimal] = mapped_column(Numeric(38, 12), default=Decimal(0))
    released_amount: Mapped[Decimal] = mapped_column(Numeric(38, 12), default=Decimal(0))
    spent_amount: Mapped[Decimal] = mapped_column(Numeric(38, 12), default=Decimal(0))
    request: Mapped[dict[str, object]] = mapped_column(JSON)
    reason: Mapped[str]
    automatic_retry: Mapped[bool] = mapped_column(default=False)
    real_provider_calls: Mapped[int] = mapped_column(default=0)


class ReservationScope(Base):
    __tablename__ = "reservation_scopes"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "scope_id"], ["budget_scopes.tenant_id", "budget_scopes.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "reservation_id"], ["budget_reservations.tenant_id", "budget_reservations.id"]
        ),
    )
    reservation_id: Mapped[UUID] = mapped_column(primary_key=True)
    scope_id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]


class ReservationOutcome(Record, Base):
    __tablename__ = "reservation_outcomes"
    __table_args__ = (UniqueConstraint("reservation_id", "outcome_key"),)
    reservation_id: Mapped[UUID] = mapped_column(ForeignKey("budget_reservations.id"))
    outcome_key: Mapped[str]
    payload: Mapped[dict[str, object]] = mapped_column(JSON)


class UsageAttempt(Record, Base):
    """Durable intent, not an additional charge. Final event is the sole cost source."""

    __tablename__ = "usage_attempts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_usage_attempts_tenant_id"),
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id"],
            ["provider_connections.tenant_id", "provider_connections.id"],
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    provider_connection_id: Mapped[UUID]
    correlation_id: Mapped[UUID]
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("identity_actors.id"))
    operation: Mapped[str]
    outcome: Mapped[str]
    business_outcome: Mapped[str] = mapped_column(default="PENDING")
    event_id: Mapped[UUID | None] = mapped_column(ForeignKey("provider_usage_events.id"))
    error_reference: Mapped[str | None]
