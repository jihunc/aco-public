"""Actual synthetic PostgreSQL source adapter; only this adapter reads source storage."""

from collections.abc import Mapping
from typing import TypeVar
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from aco.core.context import ActorContext, ServiceContext
from aco.core.types import (
    CostQuality,
    ProviderCapabilities,
    ProviderCost,
    ProviderErrorCode,
    SourceStamp,
)
from aco.features.accounting.dto import (
    AccountingDataChanged,
    AccountingResult,
    CustomerData,
    InvoiceData,
    PaymentData,
    SyncCursor,
)
from aco.features.accounting.models import DemoSourceRecord
from aco.features.identity.actors import accounting_scope
from aco.features.identity.models import ProviderConnection, now
from aco.features.identity.service import IdentityService, Permission
from aco.features.usage.accounting import ObservedAccounting
from aco.integrations.ports.accounting import failure, rejection_reference, source_rejection

T = TypeVar("T")
FEATURES = frozenset(
    {"list_customers", "list_invoices", "get_invoice", "list_payments", "get_change_feed", "synthetic"}
)


class DemoAccountingSource:
    pricing_currency = "AUD"

    def __init__(self, db: AsyncSession, connection_id: UUID):
        self.db, self.connection_id = db, connection_id

    async def describe_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(features=FEATURES)

    async def verify_webhook(
        self, raw_body: bytes, headers: Mapping[str, str]
    ) -> list[AccountingDataChanged]:
        raise failure(ProviderErrorCode.UNSUPPORTED_CAPABILITY)

    async def connection(self, ctx: ActorContext) -> ProviderConnection:
        await self.db.run_sync(lambda store: IdentityService(store).authorize(ctx, Permission.INVOICE_READ))
        await self.db.run_sync(
            lambda store: accounting_scope(store, ctx, self.connection_id, customer_read=True)
        )
        try:
            connection = await self.db.scalar(
                select(ProviderConnection).where(
                    ProviderConnection.id == self.connection_id, ProviderConnection.tenant_id == ctx.tenant_id
                )
            )
        except SQLAlchemyError as exc:
            raise failure(ProviderErrorCode.TEMPORARILY_UNAVAILABLE) from exc
        if connection is None or connection.provider_type != "accounting" or connection.provider != "demo":
            raise failure()
        if connection.status not in {"READY", "TESTED"} or not connection.external_account_id:
            raise failure(ProviderErrorCode.AUTH_EXPIRED)
        return connection

    async def rows(self, ctx: ActorContext) -> tuple[ProviderConnection, list[DemoSourceRecord]]:
        connection = await self.connection(ctx)
        scope = await self.db.run_sync(
            lambda store: accounting_scope(store, ctx, self.connection_id, customer_read=True)
        )
        query = select(DemoSourceRecord).where(
            DemoSourceRecord.tenant_id == ctx.tenant_id,
            DemoSourceRecord.provider_connection_id == self.connection_id,
        )
        if scope:
            query = query.where(
                or_(
                    and_(DemoSourceRecord.kind == "invoice", DemoSourceRecord.external_id == scope[0]),
                    and_(DemoSourceRecord.kind == "customer", DemoSourceRecord.external_id == scope[1]),
                    and_(
                        DemoSourceRecord.kind == "payment",
                        DemoSourceRecord.payload["external_invoice_id"].as_string() == scope[0],
                    ),
                )
            )
        try:
            rows = list(await self.db.scalars(query.order_by(DemoSourceRecord.sequence)))
        except SQLAlchemyError as exc:
            raise failure(ProviderErrorCode.TEMPORARILY_UNAVAILABLE) from exc
        if any(r.organisation_id != connection.external_account_id for r in rows):
            raise failure()
        return connection, rows

    def result(
        self, ctx: ActorContext, connection: ProviderConnection, value: T, cursor: SyncCursor | None = None
    ) -> AccountingResult[T]:
        return AccountingResult(
            value=value,
            tenant_id=ctx.tenant_id,
            provider_connection_id=self.connection_id,
            organisation_id=connection.external_account_id or "",
            provider="demo",
            native_operation_id=str(uuid4()),
            correlation_id=ctx.correlation_id,
            observed_at=now(),
            authenticity="VERIFIED",
            currentness="CURRENT",
            cost=ProviderCost(amount=None, currency="AUD", quality=CostQuality.UNKNOWN),
            next_cursor=cursor,
        )

    def stamp(self, row: DemoSourceRecord) -> SourceStamp:
        return SourceStamp(
            connection_id=row.provider_connection_id,
            external_id=row.external_id,
            # The ledger retains original observation; successful current read verifies now.
            # Impossible future ledger evidence cannot be laundered into freshness.
            observed_at=max(row.observed_at, now()),
            source_at=row.effective_at,
            revision=str(row.revision),
            synthetic=True,
        )

    def latest(self, rows: list[DemoSourceRecord], kind: str) -> list[DemoSourceRecord]:
        result: dict[str, DemoSourceRecord] = {}
        for row in rows:
            if row.kind == kind:
                old = result.get(row.external_id)
                if old is None or row.revision > old.revision:
                    result[row.external_id] = row
        return list(result.values())

    def cursor(self, ctx: ActorContext, sequence: int) -> SyncCursor:
        return SyncCursor(token=f"{ctx.tenant_id}:{self.connection_id}:{sequence}")

    def offset(self, ctx: ActorContext, cursor: SyncCursor | None) -> int:
        if cursor is None:
            return 0
        prefix = f"{ctx.tenant_id}:{self.connection_id}:"
        if not cursor.token.startswith(prefix):
            raise failure()
        try:
            value = int(cursor.token[len(prefix) :])
            if value < 0:
                raise ValueError()
            return value
        except ValueError as exc:
            raise failure() from exc

    async def list_customers(
        self, ctx: ActorContext, cursor: SyncCursor | None = None
    ) -> AccountingResult[list[CustomerData]]:
        self.offset(ctx, cursor)
        connection, rows = await self.rows(ctx)
        try:
            values = [
                CustomerData.model_validate(
                    {**r.payload, "stamp": self.stamp(r), "organisation_id": r.organisation_id}
                )
                for r in self.latest(rows, "customer")
            ]
        except ValidationError as exc:
            raise failure() from exc
        return self.result(ctx, connection, values)

    async def list_invoices(
        self, ctx: ActorContext, cursor: SyncCursor | None = None
    ) -> AccountingResult[list[InvoiceData]]:
        self.offset(ctx, cursor)
        connection, rows = await self.rows(ctx)
        try:
            # JSON parsing explicitly admits canonical dates/enums while preserving strict money ints.
            import json

            values = [
                InvoiceData.model_validate_json(
                    json.dumps(
                        {
                            **r.payload,
                            "stamp": self.stamp(r).model_dump(mode="json"),
                            "organisation_id": r.organisation_id,
                        }
                    )
                )
                for r in self.latest(rows, "invoice")
            ]
        except (ValidationError, ValueError) as exc:
            raise failure() from exc
        return self.result(ctx, connection, values)

    async def get_invoice(self, ctx: ActorContext, invoice_key: str) -> AccountingResult[InvoiceData]:
        await self.db.run_sync(lambda store: accounting_scope(store, ctx, self.connection_id, invoice_key))
        result = await self.list_invoices(ctx)
        match = next((v for v in result.value if v.external_id == invoice_key), None)
        if match is None:
            raise failure()
        return AccountingResult[InvoiceData](**{**result.model_dump(exclude={"value"}), "value": match})

    async def list_payments(self, ctx: ActorContext, invoice_key: str) -> AccountingResult[list[PaymentData]]:
        await self.db.run_sync(lambda store: accounting_scope(store, ctx, self.connection_id, invoice_key))
        connection, rows = await self.rows(ctx)
        candidates = [
            r for r in self.latest(rows, "payment") if r.payload.get("external_invoice_id") == invoice_key
        ]
        references = [rejection_reference(r.payload, r.external_id, str(r.revision)) for r in candidates]
        values = []
        for row, reference in zip(candidates, references, strict=True):
            try:
                values.append(
                    PaymentData.model_validate(
                        {
                            **row.payload,
                            "stamp": self.stamp(row),
                            "organisation_id": row.organisation_id,
                            "verified": True,
                        }
                    )
                )
            except ValidationError:
                raise failure(
                    rejection_evidence=source_rejection(
                        ctx,
                        connection.id,
                        invoice_key,
                        "payment_shape_invalid",
                        reference,
                        references,
                    )
                ) from None
        return self.result(ctx, connection, values)

    async def get_change_feed(
        self, ctx: ActorContext, cursor: SyncCursor | None = None
    ) -> AccountingResult[list[AccountingDataChanged]]:
        if isinstance(ctx, ServiceContext):
            raise failure()
        offset = self.offset(ctx, cursor)
        connection, rows = await self.rows(ctx)
        maximum = max((r.sequence for r in rows), default=0)
        if offset > maximum:
            raise failure()
        values = []
        invoices = self.latest(rows, "invoice")
        for row in rows:
            if row.sequence <= offset:
                continue
            keys = (
                [row.external_id]
                if row.kind == "invoice"
                else [str(row.payload["external_invoice_id"])]
                if row.kind == "payment"
                else [
                    r.external_id
                    for r in invoices
                    if r.payload.get("external_customer_id") == row.external_id
                ]
            )
            for key in keys:
                values.append(
                    AccountingDataChanged(
                        provider_connection_id=self.connection_id,
                        organisation_id=row.organisation_id,
                        external_invoice_id=key,
                        external_event_id=f"{row.sequence}:{key}",
                    )
                )
        return self.result(ctx, connection, values, self.cursor(ctx, maximum))


class DemoAccountingAdapter(ObservedAccounting):
    def __init__(self, db: AsyncSession, connection_id: UUID):
        self.demo_source = DemoAccountingSource(db, connection_id)
        super().__init__(db, self.demo_source, connection_id)

    async def connection(self, ctx: ActorContext) -> ProviderConnection:
        return await self.demo_source.connection(ctx)


class AccountingRuntime:
    """Composition factory: bind exactly the caller's active transaction."""

    def bind(self, db: AsyncSession, connection_id: UUID) -> DemoAccountingAdapter:
        return DemoAccountingAdapter(db, connection_id)
