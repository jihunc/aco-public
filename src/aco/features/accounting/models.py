"""Financial identity and append-only source evidence on the shared business metadata."""

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
    Identity,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from aco.features.identity.models import Base, Record


def connection_constraints() -> tuple[ForeignKeyConstraint, UniqueConstraint]:
    return (
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id"],
            ["provider_connections.tenant_id", "provider_connections.id"],
        ),
        UniqueConstraint("tenant_id", "provider_connection_id", "id"),
    )


class SourceOwned:
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    provider_connection_id: Mapped[UUID] = mapped_column(index=True)


class DemoSourceRecord(SourceOwned, Record, Base):
    __tablename__ = "demo_source_records"
    __table_args__ = (
        *connection_constraints(),
        UniqueConstraint("tenant_id", "provider_connection_id", "kind", "external_id", "revision"),
        CheckConstraint("revision > 0"),
    )
    sequence: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    organisation_id: Mapped[str]
    kind: Mapped[str]
    external_id: Mapped[str]
    revision: Mapped[int]
    payload: Mapped[dict[str, object]] = mapped_column(JSON)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Customer(SourceOwned, Record, Base):
    __tablename__ = "customers"
    __table_args__ = (
        *connection_constraints(),
        UniqueConstraint("tenant_id", "provider_connection_id", "external_customer_id"),
    )
    external_customer_id: Mapped[str]
    name: Mapped[str]
    source_revision: Mapped[str]
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    synthetic: Mapped[bool]


class InvoiceCustomerAssociation(Base):
    """Append-only observed projection scope; never current financial/action authority."""

    __tablename__ = "invoice_customer_associations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id", "invoice_id"],
            [
                "invoice_snapshots.tenant_id",
                "invoice_snapshots.provider_connection_id",
                "invoice_snapshots.id",
            ],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id", "customer_id"],
            ["customers.tenant_id", "customers.provider_connection_id", "customers.id"],
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(primary_key=True)
    invoice_id: Mapped[UUID] = mapped_column(primary_key=True)
    customer_id: Mapped[UUID] = mapped_column(primary_key=True)
    provider_connection_id: Mapped[UUID]
    source_revision: Mapped[str] = mapped_column(Text)
    verified_at_capture: Mapped[bool]
    capture_kind: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class CustomerContact(SourceOwned, Record, Base):
    __tablename__ = "customer_contacts"
    __table_args__ = (
        *connection_constraints(),
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id", "customer_id"],
            ["customers.tenant_id", "customers.provider_connection_id", "customers.id"],
        ),
        UniqueConstraint("tenant_id", "customer_id"),
    )
    customer_id: Mapped[UUID]
    email: Mapped[str | None]
    source_revision: Mapped[str]
    # Imported contact is not a verified recipient/permission for outreach.
    verified_for_outreach: Mapped[bool] = mapped_column(default=False)


class InvoiceSnapshot(SourceOwned, Record, Base):
    __tablename__ = "invoice_snapshots"
    __table_args__ = (
        *connection_constraints(),
        UniqueConstraint("tenant_id", "provider_connection_id", "external_invoice_id"),
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "id", "customer_id"),
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id", "customer_id"],
            ["customers.tenant_id", "customers.provider_connection_id", "customers.id"],
        ),
    )
    external_invoice_id: Mapped[str]
    customer_id: Mapped[UUID]
    number: Mapped[str]
    issue_date: Mapped[date] = mapped_column(Date)
    due_date: Mapped[date] = mapped_column(Date)
    original_minor: Mapped[int] = mapped_column(BigInteger)
    balance_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str]
    exponent: Mapped[int]
    financial_status: Mapped[str]
    source_status: Mapped[str]
    source_revision: Mapped[str]
    organisation_id: Mapped[str]
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    synthetic: Mapped[bool]
    verified: Mapped[bool]
    last_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InvoiceSourceRevision(SourceOwned, Record, Base):
    __tablename__ = "invoice_source_revisions"
    __table_args__ = (
        *connection_constraints(),
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id", "invoice_id"],
            [
                "invoice_snapshots.tenant_id",
                "invoice_snapshots.provider_connection_id",
                "invoice_snapshots.id",
            ],
        ),
        UniqueConstraint("tenant_id", "invoice_id", "source_revision"),
    )
    invoice_id: Mapped[UUID]
    source_revision: Mapped[str]
    payload: Mapped[dict[str, object]] = mapped_column(JSON)


class VerifiedPaymentAllocation(SourceOwned, Record, Base):
    __tablename__ = "verified_payment_allocations"
    __table_args__ = (
        *connection_constraints(),
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id", "invoice_id"],
            [
                "invoice_snapshots.tenant_id",
                "invoice_snapshots.provider_connection_id",
                "invoice_snapshots.id",
            ],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id", "parent_id"],
            [
                "verified_payment_allocations.tenant_id",
                "verified_payment_allocations.provider_connection_id",
                "verified_payment_allocations.id",
            ],
        ),
        UniqueConstraint(
            "tenant_id", "provider_connection_id", "external_payment_id", "external_allocation_id"
        ),
        CheckConstraint(
            "(kind = 'cash' AND minor_units > 0 AND parent_id IS NULL) OR "
            "(kind IN ('refund','reversal') AND minor_units < 0 AND parent_id IS NOT NULL) OR "
            "(kind IN ('credit','write_off') AND parent_id IS NULL)"
        ),
    )
    invoice_id: Mapped[UUID]
    external_payment_id: Mapped[str]
    external_allocation_id: Mapped[str]
    kind: Mapped[str]
    minor_units: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str]
    exponent: Mapped[int]
    parent_id: Mapped[UUID | None]
    source_revision: Mapped[str]
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    organisation_id: Mapped[str]
    synthetic: Mapped[bool]


class CollectionRecord(Record, Base):
    __tablename__ = "collection_records"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "invoice_id"], ["invoice_snapshots.tenant_id", "invoice_snapshots.id"]
        ),
        UniqueConstraint("tenant_id", "invoice_id"),
        CheckConstraint("lifecycle IN ('active','closed')"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    invoice_id: Mapped[UUID]
    lifecycle: Mapped[str] = mapped_column(default="active")
    hold_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)


class AccountingEvent(SourceOwned, Record, Base):
    __tablename__ = "accounting_events"
    __table_args__ = (
        *connection_constraints(),
        UniqueConstraint("tenant_id", "provider_connection_id", "external_event_id"),
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id", "invoice_id"],
            [
                "invoice_snapshots.tenant_id",
                "invoice_snapshots.provider_connection_id",
                "invoice_snapshots.id",
            ],
            name="fk_accounting_event_invoice",
        ),
        CheckConstraint(
            "(origin = 'SOURCE_HINT' AND external_event_id IS NOT NULL) OR "
            "(origin = 'AUTHORITATIVE_READ' AND external_event_id IS NULL)",
            name="ck_accounting_event_origin",
        ),
        Index(
            "uq_accounting_read_revision",
            "tenant_id",
            "provider_connection_id",
            "invoice_id",
            "source_revision",
            unique=True,
            postgresql_where=text("origin = 'AUTHORITATIVE_READ'"),
        ),
    )
    external_event_id: Mapped[str | None]
    origin: Mapped[str] = mapped_column(default="SOURCE_HINT", server_default="SOURCE_HINT")
    invoice_id: Mapped[UUID]
    source_revision: Mapped[str]
    envelope: Mapped[dict[str, object]] = mapped_column(JSON)
    provenance: Mapped[dict[str, object]] = mapped_column(JSON)
    result: Mapped[dict[str, object]] = mapped_column(JSON)


class DemoPaymentOperation(SourceOwned, Record, Base):
    __tablename__ = "demo_payment_operations"
    __table_args__ = (
        *connection_constraints(),
        UniqueConstraint("tenant_id", "provider_connection_id", "operation_id"),
    )
    operation_id: Mapped[UUID]
    command: Mapped[dict[str, object]] = mapped_column(JSON)
    result: Mapped[dict[str, object]] = mapped_column(JSON)


class AccountingObservation(SourceOwned, Record, Base):
    """Append-only read evidence; never overwrites original financial facts."""

    __tablename__ = "accounting_observations"
    __table_args__ = (
        *connection_constraints(),
        CheckConstraint(
            "execution_binding IS NULL OR json_typeof(execution_binding) = 'object'",
            name="ck_source_execution_object",
        ),
        UniqueConstraint("tenant_id", "invoice_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id", "invoice_id"],
            [
                "invoice_snapshots.tenant_id",
                "invoice_snapshots.provider_connection_id",
                "invoice_snapshots.id",
            ],
        ),
    )
    invoice_id: Mapped[UUID]
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("identity_actors.id"))
    external_event_id: Mapped[str | None]
    invoice_data: Mapped[dict[str, object]] = mapped_column(JSON)
    payment_data: Mapped[list[dict[str, object]]] = mapped_column(JSON)
    provenance: Mapped[dict[str, object]] = mapped_column(JSON)
    execution_binding: Mapped[dict[str, object] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DemoReset(Record, Base):
    """Immutable captured reset command/result, independent of mutable projections."""

    __tablename__ = "demo_resets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "request_id"),
        UniqueConstraint("audit_id"),
        ForeignKeyConstraint(["tenant_id", "actor_id"], ["memberships.tenant_id", "memberships.user_id"]),
        ForeignKeyConstraint(
            ["tenant_id", "audit_id"], ["identity_audit_events.tenant_id", "identity_audit_events.id"]
        ),
        CheckConstraint("previous_generation >= 1 AND generation = previous_generation + 1"),
        CheckConstraint("json_typeof(command) = 'object' AND json_typeof(result) = 'object'"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    actor_id: Mapped[UUID]
    request_id: Mapped[UUID]
    audit_id: Mapped[UUID]
    previous_generation: Mapped[int]
    generation: Mapped[int]
    command: Mapped[dict[str, object]] = mapped_column(JSON)
    result: Mapped[dict[str, object]] = mapped_column(JSON)
