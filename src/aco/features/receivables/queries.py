"""Read-only current-snapshot queries. Canonical source facts are never mutated here."""

from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator
from sqlalchemy import Nullable, Numeric, Select, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aco.core.clock import Clock, SystemClock
from aco.core.context import TenantContext
from aco.core.types import Money, SourceStamp, StrictModel
from aco.features.accounting.models import CollectionRecord, Customer, CustomerContact, InvoiceSnapshot
from aco.features.identity.models import ProviderConnection, Tenant
from aco.features.identity.service import IdentityService, Permission
from aco.features.receivables.customer_activity import (
    CustomerContactFrequency,
    CustomerInteractions,
    customer_activity,
)
from aco.features.receivables.next_action import NextSavedAction, next_saved_actions


class MoneyJSON(Money):
    """Read-only wire projection; validation and arithmetic retain canonical Money integers."""

    @field_serializer("minor_units", when_used="json")
    def exact_minor_units(self, value: int) -> str:
        return str(value)


WireMoney = MoneyJSON
Freshness = Literal["fresh", "stale", "unknown"]


class ReceivableFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    search: str = Field(default="", max_length=120)
    customer_id: UUID | None = None
    overdue: bool | None = None
    financial_status: (
        Literal["open", "partially_paid", "paid", "voided", "credited", "written_off", "unknown"] | None
    ) = None
    collection_state: Literal["active", "closed", "held"] | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    min_minor: str | None = Field(default=None, pattern=r"^-?[0-9]{1,30}$")
    max_minor: str | None = Field(default=None, pattern=r"^-?[0-9]{1,30}$")
    stale: bool | None = None
    page: int = Field(default=1, ge=1, le=1000000)
    page_size: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def amount_currency(self) -> "ReceivableFilter":
        if (self.min_minor is not None or self.max_minor is not None) and self.currency is None:
            raise ValueError("Select a currency for amount filters")
        if (
            self.min_minor is not None
            and self.max_minor is not None
            and int(self.min_minor) > int(self.max_minor)
        ):
            raise ValueError("Minimum exceeds maximum")
        return self


class MetricQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")


class CustomerFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    search: str = Field(default="", max_length=120)
    page: int = Field(default=1, ge=1, le=1000000)
    page_size: int = Field(default=20, ge=1, le=100)


class Page[T](StrictModel):
    items: list[T]
    total: int
    page: int
    page_size: int
    as_of_at: datetime


class CustomerView(StrictModel):
    id: UUID
    name: str
    provider_connection_id: UUID
    external_customer_id: str
    synthetic: bool
    source: SourceStamp


class InvoiceView(StrictModel):
    id: UUID
    number: str
    customer: CustomerView
    issue_date: str
    due_date: str
    original: WireMoney
    outstanding: WireMoney
    financial_status: str
    source_status: str
    collection_lifecycle: str | None
    hold_reasons: list[str] | None
    source: SourceStamp
    accounting_provider: str
    organisation_id: str
    last_synced_at: datetime | None
    verified: bool
    freshness: Freshness
    overdue: bool
    days_past_due: int
    next_action: NextSavedAction = Field(default_factory=NextSavedAction)


class InvoiceWorkspace(StrictModel):
    invoice: InvoiceView
    as_of_at: datetime
    sections: dict[str, Literal["available", "unavailable"]]
    allowed_actions: list[str] = []


class CustomerWorkspace(StrictModel):
    customer: CustomerView
    contact_email: str | None
    verified_for_outreach: bool
    interactions: CustomerInteractions
    contact_frequency: CustomerContactFrequency


class Aging(StrictModel):
    current: str
    days_1_30: str
    days_31_60: str
    days_61_90: str
    days_91_plus: str
    total: str


class CurrencyOperations(StrictModel):
    currency: str
    exponent: int
    outstanding: WireMoney | None
    overdue: WireMoney | None
    overdue_ratio: str | None
    aging: Aging | None
    invoice_count: int
    included_count: int
    excluded_count: int
    excluded_balance: WireMoney
    excluded_by_reason: dict[str, int]
    freshness: Freshness
    last_synced_at: datetime | None
    completeness: Literal["complete", "provisional", "incomplete"]
    provenance: Literal["synthetic", "observed", "mixed"]


class OperationsView(StrictModel):
    currencies: list[CurrencyOperations]
    as_of_at: datetime
    timezone: str
    basis: str = "Current imported positive verified open/partial balances; tenant-calendar aging; no FX."
    availability: Literal["available", "unavailable"]
    allowed_actions: list[str] = []
    workflow_availability: Literal["available", "unavailable"] = "available"


def customer_view(row: Customer) -> CustomerView:
    return CustomerView(
        id=row.id,
        name=row.name,
        provider_connection_id=row.provider_connection_id,
        external_customer_id=row.external_customer_id,
        synthetic=row.synthetic,
        source=SourceStamp(
            connection_id=row.provider_connection_id,
            external_id=row.external_customer_id,
            observed_at=row.observed_at,
            source_at=None,
            revision=row.source_revision,
            synthetic=row.synthetic,
        ),
    )


def freshness(row: InvoiceSnapshot, at: datetime) -> Freshness:
    if not row.verified or row.last_read_at is None or row.last_read_at > at or row.effective_at > at:
        return "unknown"
    return "stale" if at - row.last_read_at > timedelta(minutes=15) else "fresh"


class ReceivableQueries:
    def __init__(self, db: AsyncSession, clock: Clock | None = None):
        self.db = db
        self.clock = clock or SystemClock()

    async def tenant(self, ctx: TenantContext) -> Tenant:
        if not isinstance(ctx, TenantContext):
            raise HTTPException(403, "HUMAN_CONTEXT_REQUIRED")
        # Re-read persisted Membership even for direct service consumers; caller role is not authority.
        return await self.db.run_sync(
            lambda store: IdentityService(store).authorize(ctx, Permission.INVOICE_READ)
        )

    def rows(
        self, ctx: TenantContext
    ) -> Select[InvoiceSnapshot, Customer, CollectionRecord | None, ProviderConnection]:
        return (
            select(InvoiceSnapshot, Customer, Nullable(CollectionRecord), ProviderConnection)
            .join(
                Customer,
                (Customer.id == InvoiceSnapshot.customer_id)
                & (Customer.tenant_id == InvoiceSnapshot.tenant_id),
            )
            .outerjoin(
                CollectionRecord,
                (CollectionRecord.invoice_id == InvoiceSnapshot.id)
                & (CollectionRecord.tenant_id == InvoiceSnapshot.tenant_id),
            )
            .join(
                ProviderConnection,
                (ProviderConnection.id == InvoiceSnapshot.provider_connection_id)
                & (ProviderConnection.tenant_id == InvoiceSnapshot.tenant_id),
            )
            .where(InvoiceSnapshot.tenant_id == ctx.tenant_id)
        )

    def view(
        self,
        row: InvoiceSnapshot,
        customer: Customer,
        collection: CollectionRecord | None,
        connection: ProviderConnection,
        at: datetime,
        timezone: str,
        next_action: NextSavedAction | None = None,
    ) -> InvoiceView:
        days = (at.astimezone(ZoneInfo(timezone)).date() - row.due_date).days
        return InvoiceView(
            id=row.id,
            next_action=next_action or NextSavedAction(),
            number=row.number,
            customer=customer_view(customer),
            issue_date=str(row.issue_date),
            due_date=str(row.due_date),
            original=MoneyJSON(minor_units=row.original_minor, currency=row.currency, exponent=row.exponent),
            outstanding=MoneyJSON(
                minor_units=row.balance_minor, currency=row.currency, exponent=row.exponent
            ),
            financial_status=row.financial_status,
            source_status=row.source_status,
            collection_lifecycle=collection.lifecycle if collection else None,
            hold_reasons=collection.hold_reasons if collection else None,
            source=SourceStamp(
                connection_id=row.provider_connection_id,
                external_id=row.external_invoice_id,
                observed_at=row.observed_at,
                source_at=row.effective_at,
                revision=row.source_revision,
                synthetic=row.synthetic,
            ),
            accounting_provider=connection.provider,
            organisation_id=row.organisation_id,
            last_synced_at=row.last_read_at,
            verified=row.verified,
            freshness=freshness(row, at),
            overdue=days > 0 and row.balance_minor > 0 and row.financial_status in {"open", "partially_paid"},
            days_past_due=days,
        )

    async def list_receivables(self, ctx: TenantContext, filters: ReceivableFilter) -> Page[InvoiceView]:
        tenant = await self.tenant(ctx)
        at = self.clock.now()
        query = self.rows(ctx)
        if filters.search:
            # Literal substring, not SQL wildcard syntax.
            term = "%" + filters.search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            query = query.where(
                or_(InvoiceSnapshot.number.ilike(term, escape="\\"), Customer.name.ilike(term, escape="\\"))
            )
        if filters.customer_id:
            query = query.where(InvoiceSnapshot.customer_id == filters.customer_id)
        if filters.currency:
            query = query.where(InvoiceSnapshot.currency == filters.currency)
        if filters.financial_status:
            query = query.where(InvoiceSnapshot.financial_status == filters.financial_status)
        if filters.collection_state == "held":
            query = query.where(func.json_array_length(CollectionRecord.hold_reasons) > 0)
        elif filters.collection_state:
            query = query.where(CollectionRecord.lifecycle == filters.collection_state)
        for amount, operation in ((filters.min_minor, "min"), (filters.max_minor, "max")):
            if amount is not None:
                query = query.where(
                    InvoiceSnapshot.balance_minor >= literal(Decimal(amount), type_=Numeric())
                    if operation == "min"
                    else InvoiceSnapshot.balance_minor <= literal(Decimal(amount), type_=Numeric())
                )
        due = (
            (InvoiceSnapshot.due_date < at.astimezone(ZoneInfo(tenant.timezone)).date())
            & (InvoiceSnapshot.balance_minor > 0)
            & InvoiceSnapshot.financial_status.in_(["open", "partially_paid"])
        )
        if filters.overdue is not None:
            query = query.where(due if filters.overdue else ~due)
        current = (
            InvoiceSnapshot.verified.is_(True)
            & InvoiceSnapshot.last_read_at.is_not(None)
            & (InvoiceSnapshot.last_read_at <= at)
            & (InvoiceSnapshot.last_read_at >= at - timedelta(minutes=15))
            & (InvoiceSnapshot.effective_at <= at)
        )
        if filters.stale is not None:
            query = query.where(current if not filters.stale else ~func.coalesce(current, False))
        total = await self.db.scalar(select(func.count()).select_from(query.subquery()))
        rows = (
            await self.db.execute(
                query.order_by(InvoiceSnapshot.number, InvoiceSnapshot.id)
                .offset((filters.page - 1) * filters.page_size)
                .limit(filters.page_size)
            )
        ).all()
        pending = await next_saved_actions(self.db, ctx.tenant_id, [r[0].id for r in rows])
        return Page(
            items=[self.view(*r, at, tenant.timezone, pending.get(r[0].id)) for r in rows],
            total=total or 0,
            page=filters.page,
            page_size=filters.page_size,
            as_of_at=at,
        )

    async def get_invoice_workspace(self, ctx: TenantContext, invoice_id: UUID) -> InvoiceWorkspace:
        tenant = await self.tenant(ctx)
        row = (await self.db.execute(self.rows(ctx).where(InvoiceSnapshot.id == invoice_id))).first()
        if row is None:
            raise HTTPException(404, "INVOICE_NOT_FOUND")
        at = self.clock.now()
        pending = await next_saved_actions(self.db, ctx.tenant_id, [invoice_id])
        return InvoiceWorkspace(
            invoice=self.view(*row, at, tenant.timezone, pending.get(invoice_id)),
            as_of_at=at,
            sections={
                **dict.fromkeys(["communications", "cases", "promises", "tasks"], "available"),
                "audit": "available",
            },
        )

    async def list_customers(self, ctx: TenantContext, filters: CustomerFilter) -> Page[CustomerView]:
        await self.tenant(ctx)
        query = select(Customer).where(Customer.tenant_id == ctx.tenant_id)
        if filters.search:
            query = query.where(func.strpos(func.lower(Customer.name), filters.search.lower()) > 0)
        total = await self.db.scalar(select(func.count()).select_from(query.subquery()))
        rows = await self.db.scalars(
            query.order_by(Customer.name, Customer.id)
            .offset((filters.page - 1) * filters.page_size)
            .limit(filters.page_size)
        )
        return Page(
            items=[customer_view(r) for r in rows],
            total=total or 0,
            page=filters.page,
            page_size=filters.page_size,
            as_of_at=self.clock.now(),
        )

    async def get_customer(self, ctx: TenantContext, customer_id: UUID) -> CustomerWorkspace:
        await self.tenant(ctx)
        row = await self.db.scalar(
            select(Customer).where(Customer.tenant_id == ctx.tenant_id, Customer.id == customer_id)
        )
        if row is None:
            raise HTTPException(404, "CUSTOMER_NOT_FOUND")
        contact = await self.db.scalar(
            select(CustomerContact).where(
                CustomerContact.tenant_id == ctx.tenant_id, CustomerContact.customer_id == customer_id
            )
        )
        interactions, frequency = await self.db.run_sync(
            lambda store: customer_activity(store, ctx, customer_id, self.clock.now())
        )
        return CustomerWorkspace(
            interactions=interactions,
            contact_frequency=frequency,
            customer=customer_view(row),
            contact_email=contact.email if contact else None,
            verified_for_outreach=contact.verified_for_outreach if contact else False,
        )

    async def get_operations(self, ctx: TenantContext, query: MetricQuery) -> OperationsView:
        tenant = await self.tenant(ctx)
        at = self.clock.now()
        statement = select(InvoiceSnapshot).where(InvoiceSnapshot.tenant_id == ctx.tenant_id)
        if query.currency:
            statement = statement.where(InvoiceSnapshot.currency == query.currency)
        groups: dict[tuple[str, int], list[InvoiceSnapshot]] = {}
        for row in await self.db.scalars(statement):
            groups.setdefault((row.currency, row.exponent), []).append(row)
        currencies = []
        for (currency, exponent), rows in sorted(groups.items()):
            bins = [0] * 5
            excluded: dict[str, int] = {}
            excluded_balance = 0
            count = 0
            for row in rows:
                reason = (
                    "unverified_or_unknown"
                    if not row.verified or row.financial_status == "unknown" or row.effective_at > at
                    else row.financial_status
                    if row.financial_status not in {"open", "partially_paid", "paid"}
                    else "negative_balance"
                    if row.balance_minor < 0
                    else "zero_balance"
                    if row.balance_minor == 0
                    else None
                )
                if reason:
                    excluded[reason] = excluded.get(reason, 0) + 1
                    excluded_balance += row.balance_minor
                    continue
                days = (at.astimezone(ZoneInfo(tenant.timezone)).date() - row.due_date).days
                bucket = 0 if days <= 0 else 1 if days <= 30 else 2 if days <= 60 else 3 if days <= 90 else 4
                bins[bucket] += row.balance_minor
                count += 1
            states = [freshness(r, at) for r in rows]
            fresh: Freshness = "unknown" if "unknown" in states else "stale" if "stale" in states else "fresh"
            total = sum(bins)
            overdue = sum(bins[1:])
            ratio = None
            if total > 0:
                with localcontext() as decimal_context:
                    decimal_context.prec = 40
                    decimal_context.rounding = ROUND_HALF_EVEN
                    ratio = str(Decimal(overdue) / Decimal(total))
            provenance: Literal["synthetic", "observed", "mixed"] = (
                "synthetic"
                if all(r.synthetic for r in rows)
                else "observed"
                if not any(r.synthetic for r in rows)
                else "mixed"
            )
            supported = any(
                r.verified and r.financial_status != "unknown" and r.effective_at <= at for r in rows
            )
            currencies.append(
                CurrencyOperations(
                    currency=currency,
                    exponent=exponent,
                    outstanding=MoneyJSON(minor_units=total, currency=currency, exponent=exponent)
                    if supported
                    else None,
                    overdue=MoneyJSON(minor_units=overdue, currency=currency, exponent=exponent)
                    if supported
                    else None,
                    overdue_ratio=ratio,
                    aging=Aging(
                        current=str(bins[0]),
                        days_1_30=str(bins[1]),
                        days_31_60=str(bins[2]),
                        days_61_90=str(bins[3]),
                        days_91_plus=str(bins[4]),
                        total=str(total),
                    )
                    if supported
                    else None,
                    invoice_count=len(rows),
                    included_count=count,
                    excluded_count=sum(excluded.values()),
                    excluded_balance=MoneyJSON(
                        minor_units=excluded_balance, currency=currency, exponent=exponent
                    ),
                    excluded_by_reason=excluded,
                    freshness=fresh,
                    last_synced_at=min(r.last_read_at for r in rows if r.last_read_at is not None)
                    if all(r.last_read_at is not None for r in rows)
                    else None,
                    completeness="incomplete"
                    if fresh == "unknown" or "unverified_or_unknown" in excluded
                    else "provisional"
                    if fresh == "stale"
                    else "complete",
                    provenance=provenance,
                )
            )
        return OperationsView(
            currencies=currencies,
            as_of_at=at,
            timezone=tenant.timezone,
            availability="available" if currencies else "unavailable",
        )
