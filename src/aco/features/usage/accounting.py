"""Durable accounting boundary evidence; no change to the caller's business transaction."""

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, TypeVar
from uuid import UUID, uuid4

from fastapi import HTTPException
from iso4217 import Currency
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, SessionTransaction

from aco.core.context import ActorContext, ServiceContext
from aco.core.types import CostQuality, ProviderCost
from aco.features.accounting.dto import AccountingResult, SyncCursor
from aco.features.identity.actors import accounting_scope
from aco.features.identity.models import ProviderConnection, Tenant
from aco.features.identity.service import IdentityService, Permission
from aco.features.usage.dto import ProviderUsageInput
from aco.features.usage.models import UsageAttempt
from aco.features.usage.service import UsageService
from aco.integrations.ports.accounting import AccountingFailure, AccountingPort, failure

T = TypeVar("T")


def _current_business_transaction(db: Session) -> SessionTransaction | None:
    return db.get_nested_transaction() or db.get_transaction()


def _complete_business_transaction(db: Session, transaction: SessionTransaction, outcome: str) -> None:
    tracking: dict[SessionTransaction, list[UUID]] = db.info.get("usage_attempt_ids", {})
    attempts = tracking.pop(transaction, [])
    if not attempts:
        return
    if transaction.nested and outcome == "COMMITTED":
        # SAVEPOINT release preserves the operation in its parent, but does not
        # prove that parent (or any ancestor) will commit.
        parent = transaction.parent
        assert parent is not None
        tracking.setdefault(parent, []).extend(attempts)
        return
    # SQLAlchemy's async bridge adapts this maintained sync event hook in its greenlet.
    # A failed independent notification leaves durable PENDING, never a false
    # business outcome or permission to replay the provider operation.
    with Session(db.get_bind()) as separate, separate.begin():
        separate.execute(text("SET LOCAL lock_timeout = '2s'"))
        for key in attempts:
            row = separate.get(UsageAttempt, key)
            if row is not None:
                row.business_outcome = outcome


def _business_outcome(db: Session, outcome: str) -> None:
    transaction = _current_business_transaction(db)
    if transaction is not None:
        _complete_business_transaction(db, transaction, outcome)


def _unresolved_business(db: Session, transaction: SessionTransaction | None) -> None:
    # Discard only this notification; other savepoints/outer attempts still need
    # their own outcome. Durable evidence remains PENDING after writer failure.
    db.info.get("usage_attempt_ids", {}).pop(transaction, None)
    logging.getLogger(__name__).warning("Business transaction outcome unresolved in usage evidence")


@event.listens_for(Session, "after_commit")
def _committed(db: Session) -> None:
    try:
        _business_outcome(db, "COMMITTED")
    except Exception:
        # Financial COMMIT already succeeded; an evidence outage must not escape
        # as an apparent business failure or become a later rollback notification.
        _unresolved_business(db, _current_business_transaction(db))


@event.listens_for(Session, "after_rollback")
def _rolled_back(db: Session) -> None:
    try:
        _business_outcome(db, "ROLLED_BACK")
    except Exception:
        _unresolved_business(db, _current_business_transaction(db))


@event.listens_for(Session, "after_transaction_end")
def _transaction_ended(db: Session, transaction: SessionTransaction) -> None:
    # Session.close/root rollback can close descendant savepoints without a
    # descendant after_rollback callback. A successful release already moved
    # its attempts above; any still attached here belonged to discarded work.
    try:
        _complete_business_transaction(db, transaction, "ROLLED_BACK")
    except Exception:
        _unresolved_business(db, transaction)


class ObservedAccounting:
    def __init__(self, db: AsyncSession, source: AccountingPort, connection_id: UUID):
        self.db, self.source, self.connection_id = db, source, connection_id
        self.pricing_currency: str | None = getattr(source, "pricing_currency", None)
        self.sessions = async_sessionmaker(db.bind, expire_on_commit=False)

    async def invoke(
        self,
        ctx: ActorContext,
        operation: str,
        call: Callable[[], Awaitable[AccountingResult[T]]],
        invoice_key: str | None = None,
    ) -> AccountingResult[T]:
        await self.db.run_sync(lambda store: IdentityService(store).authorize(ctx, Permission.INVOICE_READ))
        if isinstance(ctx, ServiceContext):
            if operation not in {"get_invoice", "list_payments", "list_customers"}:
                raise HTTPException(403, "ACCOUNTING_SCOPE_OUTSIDE_ACTION")
            await self.db.run_sync(
                lambda store: accounting_scope(
                    store, ctx, self.connection_id, invoice_key, customer_read=operation == "list_customers"
                )
            )
        key = uuid4()
        async with self.sessions.begin() as evidence:
            await evidence.execute(text("SET LOCAL lock_timeout = '2s'"))
            connection = await evidence.scalar(
                select(ProviderConnection).where(
                    ProviderConnection.id == self.connection_id, ProviderConnection.tenant_id == ctx.tenant_id
                )
            )
            tenant = await evidence.get(Tenant, ctx.tenant_id)
            if connection is None or tenant is None:
                raise HTTPException(409, "COMMITTED_PROVIDER_SETUP_REQUIRED")
            provider = connection.provider
            try:
                Currency(self.pricing_currency)
            except (ValueError, TypeError):
                raise HTTPException(409, "PRICING_CURRENCY_REQUIRED") from None
            # Demo is synthetic; other accounting adapters supply explicit source provenance.
            synthetic = provider in {"demo", "document-test", "mock"}
            evidence.add(
                UsageAttempt(
                    id=key,
                    tenant_id=ctx.tenant_id,
                    provider_connection_id=self.connection_id,
                    correlation_id=ctx.correlation_id,
                    actor_id=ctx.actor_id,
                    operation=operation,
                    outcome="PENDING",
                )
            )
        transaction = _current_business_transaction(self.db.sync_session)
        assert transaction is not None
        self.db.info.setdefault("usage_attempt_ids", {}).setdefault(transaction, []).append(key)
        try:
            result = await call()
            # Organization may be initialized in caller's atomic seed; verify against
            # that current stored connection, not the independent NOT_CONFIGURED copy.
            current = await self.db.get(ProviderConnection, self.connection_id)
            if (
                current is None
                or result.tenant_id != ctx.tenant_id
                or result.provider_connection_id != self.connection_id
                or result.provider != provider
                or result.cost.currency != self.pricing_currency
                or result.correlation_id != ctx.correlation_id
                or result.organisation_id != current.external_account_id
            ):
                raise failure()
        except Exception as error:
            code = (
                error.failure.code.value
                if isinstance(error, AccountingFailure)
                else "TEMPORARILY_UNAVAILABLE"
            )
            reference = error.failure.request_id if isinstance(error, AccountingFailure) else None
            await self.finish(ctx, key, operation, provider, synthetic, None, code, reference)
            raise
        await self.finish(ctx, key, operation, provider, synthetic, result, "VERIFIED", None)
        return result

    async def finish(
        self,
        ctx: ActorContext,
        key: UUID,
        operation: str,
        provider: str,
        synthetic: bool,
        result: AccountingResult[Any] | None,
        outcome: str,
        reference: str | None,
    ) -> None:
        async with self.sessions.begin() as evidence:
            attempt = await evidence.get(UsageAttempt, key, with_for_update=True)
            assert attempt is not None
            native = result.native_operation_id if result is not None else None
            assert self.pricing_currency is not None
            cost = (
                result.cost
                if result is not None
                else ProviderCost(amount=None, currency=self.pricing_currency, quality=CostQuality.UNKNOWN)
            )
            value = ProviderUsageInput(
                provider_connection_id=self.connection_id,
                operation_key="native:" + native if native else "attempt:" + str(key),
                component="accounting",
                operation=operation,
                provider_kind="accounting",
                provider=provider,
                synthetic=synthetic,
                cost=cost,
                native_operation_id=native,
                outcome=outcome,
            )
            usage = await evidence.run_sync(lambda store: UsageService(store).record_verified(ctx, value))
            attempt.event_id, attempt.outcome, attempt.error_reference = usage.id, outcome, reference

    async def list_customers(self, ctx: ActorContext, cursor: SyncCursor | None = None) -> Any:
        return await self.invoke(ctx, "list_customers", lambda: self.source.list_customers(ctx, cursor))

    async def list_invoices(self, ctx: ActorContext, cursor: SyncCursor | None = None) -> Any:
        return await self.invoke(ctx, "list_invoices", lambda: self.source.list_invoices(ctx, cursor))

    async def get_invoice(self, ctx: ActorContext, invoice_key: str) -> Any:
        return await self.invoke(
            ctx, "get_invoice", lambda: self.source.get_invoice(ctx, invoice_key), invoice_key
        )

    async def list_payments(self, ctx: ActorContext, invoice_key: str) -> Any:
        return await self.invoke(
            ctx, "list_payments", lambda: self.source.list_payments(ctx, invoice_key), invoice_key
        )

    async def get_change_feed(self, ctx: ActorContext, cursor: SyncCursor | None = None) -> Any:
        return await self.invoke(ctx, "get_change_feed", lambda: self.source.get_change_feed(ctx, cursor))

    async def describe_capabilities(self) -> Any:
        return await self.source.describe_capabilities()  # local metadata, no provider request/fee

    async def verify_webhook(self, raw_body: bytes, headers: Mapping[str, str]) -> Any:
        # Local unsupported validation never fabricates a vendor request.
        return await self.source.verify_webhook(raw_body, headers)
