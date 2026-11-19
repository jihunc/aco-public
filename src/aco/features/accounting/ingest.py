"""Async source reconciliation inside the caller's single atomic business transaction."""

import json
from collections.abc import Callable
from copy import deepcopy
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aco.core.clock import Clock, SystemClock
from aco.core.context import ActorContext, ServiceContext
from aco.core.events import EventEnvelope
from aco.features.accounting.dto import (
    AccountingDataChanged,
    AccountingResult,
    PaymentData,
    ReconciliationResult,
    SyncCursor,
    SyncResult,
)
from aco.features.accounting.models import (
    AccountingEvent,
    AccountingObservation,
    CollectionRecord,
    Customer,
    CustomerContact,
    InvoiceSnapshot,
    InvoiceSourceRevision,
    VerifiedPaymentAllocation,
)
from aco.features.identity.actors import accounting_scope
from aco.features.identity.models import ProviderConnection
from aco.features.identity.service import IdentityService, Permission
from aco.integrations.ports.accounting import (
    AccountingFailure,
    AccountingPort,
    RejectionReason,
    failure,
    rejection_reference,
    source_rejection,
)
from aco.integrations.ports.identity import AuthenticatedSubject


class AccountingService:
    def __init__(self, db: AsyncSession, adapter: AccountingPort, clock: Clock | None = None):
        from aco.features.usage.accounting import ObservedAccounting

        self.db, self.clock = db, clock or SystemClock()
        self.last_observation_id: UUID | None = None
        self.adapter = (
            adapter
            if isinstance(adapter, ObservedAccounting)
            else ObservedAccounting(db, adapter, adapter.connection_id)
        )

    async def authorize(self, ctx: ActorContext) -> None:
        await self.db.run_sync(lambda store: IdentityService(store).authorize(ctx, Permission.DEMO_PAYMENT))

    async def recheck_invoice(self, ctx: ActorContext, invoice_key: str) -> ReconciliationResult | None:
        """One stored operation's source scope; never expands a worker into tenant-wide sync."""
        from aco.core.context import ServiceContext
        from aco.features.actions.models import ScheduledAction

        await self.authorize(ctx)
        if isinstance(ctx, ServiceContext):
            action = await self.db.get(ScheduledAction, ctx.action_id)
            invoice = await self.db.get(InvoiceSnapshot, action.invoice_id) if action else None
            if invoice is None or invoice.external_invoice_id != invoice_key:
                raise failure()
        return await self._reconcile(ctx, invoice_key, None)

    async def validate_result[T](self, ctx: ActorContext, result: AccountingResult[T]) -> ProviderConnection:
        connection = await self.db.scalar(
            select(ProviderConnection).where(
                ProviderConnection.id == result.provider_connection_id,
                ProviderConnection.tenant_id == ctx.tenant_id,
            )
        )
        if (
            connection is None
            or connection.provider_type != "accounting"
            or result.provider != connection.provider
            or result.tenant_id != ctx.tenant_id
            or result.correlation_id != ctx.correlation_id
            or result.organisation_id != connection.external_account_id
            or result.authenticity != "VERIFIED"
        ):
            raise failure()
        return connection

    def require_current[T](self, result: AccountingResult[T]) -> None:
        if result.currentness != "CURRENT" or not (
            timedelta(0) <= self.clock.now() - result.observed_at <= timedelta(minutes=15)
        ):
            raise failure()

    @staticmethod
    def financial_payload(payload: dict[str, object]) -> dict[str, object]:
        facts = deepcopy(payload)
        facts.pop("verified", None)
        stamp = facts.get("stamp")
        if isinstance(stamp, dict):
            stamp.pop("observed_at", None)
        return facts

    async def customers(self, ctx: ActorContext, invoice_key: str | None = None) -> None:
        scope = await self.db.run_sync(
            lambda store: accounting_scope(store, ctx, self.adapter.connection_id, invoice_key)
        )
        cursor = None
        seen: set[str] = set()
        while True:
            result = await self.adapter.list_customers(ctx, cursor)
            await self.validate_result(ctx, result)
            if scope and any(value.external_id != scope[1] for value in result.value):
                raise failure()
            for value in result.value:
                if (
                    value.stamp.connection_id != result.provider_connection_id
                    or value.organisation_id != result.organisation_id
                ):
                    raise failure()
                row = await self.db.scalar(
                    select(Customer).where(
                        Customer.tenant_id == ctx.tenant_id,
                        Customer.provider_connection_id == result.provider_connection_id,
                        Customer.external_customer_id == value.external_id,
                    )
                )
                if row is None:
                    row = Customer(
                        tenant_id=ctx.tenant_id,
                        provider_connection_id=result.provider_connection_id,
                        external_customer_id=value.external_id,
                    )
                    self.db.add(row)
                row.name, row.source_revision = value.name, value.stamp.revision or "unknown"
                row.observed_at, row.synthetic = value.stamp.observed_at, value.stamp.synthetic
                await self.db.flush()
                contact = await self.db.scalar(
                    select(CustomerContact).where(
                        CustomerContact.tenant_id == ctx.tenant_id, CustomerContact.customer_id == row.id
                    )
                )
                if contact is None:
                    contact = CustomerContact(
                        tenant_id=ctx.tenant_id,
                        provider_connection_id=result.provider_connection_id,
                        customer_id=row.id,
                    )
                    self.db.add(contact)
                contact.email, contact.source_revision = value.email, row.source_revision
            await self.db.flush()
            cursor = result.next_cursor
            if cursor is None:
                return
            if cursor.token in seen:
                raise failure()
            seen.add(cursor.token)

    async def apply_accounting_change(
        self, ctx: ActorContext, event: AccountingDataChanged
    ) -> ReconciliationResult:
        if isinstance(ctx, ServiceContext):
            raise failure()
        result = await self._reconcile(ctx, event.external_invoice_id, event)
        assert result is not None
        return result

    async def _reconcile(
        self, ctx: ActorContext, invoice_key: str, event: AccountingDataChanged | None
    ) -> ReconciliationResult | None:
        await self.authorize(ctx)
        await self.db.run_sync(
            lambda store: accounting_scope(store, ctx, self.adapter.connection_id, invoice_key)
        )
        source = await self.adapter.get_invoice(ctx, invoice_key)
        connection = await self.validate_result(ctx, source)
        self.require_current(source)
        if event is not None and (
            event.provider_connection_id != connection.id
            or event.organisation_id != connection.external_account_id
        ):
            raise failure()
        invoice = source.value
        if (
            invoice.stamp.connection_id != connection.id
            or invoice.external_id != invoice_key
            or invoice.organisation_id != connection.external_account_id
            or invoice.stamp.source_at is None
            or invoice.stamp.revision is None
        ):
            raise failure()
        previous = None
        if event is not None:
            previous = await self.db.scalar(
                select(AccountingEvent).where(
                    AccountingEvent.tenant_id == ctx.tenant_id,
                    AccountingEvent.provider_connection_id == connection.id,
                    AccountingEvent.external_event_id == event.external_event_id,
                )
            )
            if previous is not None and previous.envelope["payload"] != event.model_dump(mode="json"):
                raise failure()
        await self.customers(ctx, invoice_key)
        customer = await self.db.scalar(
            select(Customer).where(
                Customer.tenant_id == ctx.tenant_id,
                Customer.provider_connection_id == connection.id,
                Customer.external_customer_id == invoice.external_customer_id,
            )
        )
        if customer is None:
            raise failure()
        # Complete the bounded provider reads against the scheduling revision.
        # Applying our own verified invoice revision before the last read would
        # invalidate that read's unchanged action grant. Revalidate after all
        # reads, then apply the projection atomically under the authority lock.
        payments = await self.adapter.list_payments(ctx, invoice.external_id)
        await self.validate_result(ctx, payments)
        self.require_current(payments)
        if payments.provider_connection_id != connection.id:
            raise failure()
        if isinstance(ctx, ServiceContext):
            await self.authorize(ctx)
        row = await self.db.scalar(
            select(InvoiceSnapshot).where(
                InvoiceSnapshot.tenant_id == ctx.tenant_id,
                InvoiceSnapshot.provider_connection_id == connection.id,
                InvoiceSnapshot.external_invoice_id == invoice.external_id,
            )
        )
        new_snapshot = row is None
        if row is None:
            row = InvoiceSnapshot(
                id=uuid4(),
                tenant_id=ctx.tenant_id,
                provider_connection_id=connection.id,
                external_invoice_id=invoice.external_id,
            )
            self.db.add(row)
        elif row.last_read_at is not None and source.observed_at < row.last_read_at:
            raise failure()  # Old read response cannot replace a later verified current read.
        fresh = invoice.verified and timedelta(
            0
        ) <= self.clock.now() - invoice.stamp.observed_at <= timedelta(minutes=15)
        # Unknown/stale/future source evidence cannot replace accepted financial facts.
        # A first unavailable projection is explicitly unknown until verified.
        execution = None
        if isinstance(ctx, ServiceContext):
            from aco.features.accounting.reconcile import execution_binding

            execution = await self.db.run_sync(lambda store: execution_binding(store, ctx))
        if fresh or new_snapshot:
            row.customer_id, row.number = customer.id, invoice.number
            row.issue_date, row.due_date = invoice.issue_date, invoice.due_date
            row.original_minor, row.balance_minor = (
                invoice.original.minor_units,
                invoice.outstanding.minor_units,
            )
            row.currency, row.exponent = invoice.original.currency, invoice.original.exponent
            row.source_status, row.source_revision = invoice.source_status, invoice.stamp.revision
            row.organisation_id = invoice.organisation_id
            row.effective_at, row.observed_at = invoice.stamp.source_at, invoice.stamp.observed_at
            row.synthetic = invoice.stamp.synthetic
        row.financial_status, row.verified = invoice.status if fresh else "unknown", fresh
        if fresh:
            row.last_read_at = source.observed_at
        await self.db.flush()
        payload = invoice.model_dump(mode="json")
        new_revision = False
        if fresh:
            revision = await self.db.scalar(
                select(InvoiceSourceRevision).where(
                    InvoiceSourceRevision.tenant_id == ctx.tenant_id,
                    InvoiceSourceRevision.invoice_id == row.id,
                    InvoiceSourceRevision.source_revision == invoice.stamp.revision,
                )
            )
            if revision is None:
                new_revision = True
                self.db.add(
                    InvoiceSourceRevision(
                        tenant_id=ctx.tenant_id,
                        provider_connection_id=connection.id,
                        invoice_id=row.id,
                        source_revision=invoice.stamp.revision,
                        payload=payload,
                    )
                )
            elif self.financial_payload(revision.payload) != self.financial_payload(payload):
                raise failure()  # Reobservation is not a license to rewrite financial facts.

        def rejected(value: PaymentData, reason: RejectionReason) -> AccountingFailure:
            # Envelope identity is verified above; only this exact requested resource may contribute refs.
            scoped = [
                v
                for v in payments.value
                if (
                    v.stamp.connection_id == connection.id
                    and v.external_invoice_id == invoice.external_id
                    and v.organisation_id == invoice.organisation_id
                )
            ]
            return failure(
                rejection_evidence=source_rejection(
                    ctx,
                    connection.id,
                    invoice.external_id,
                    reason,
                    rejection_reference(value.model_dump(), value.stamp.external_id, value.stamp.revision),
                    [
                        rejection_reference(v.model_dump(), v.stamp.external_id, v.stamp.revision)
                        for v in scoped
                    ],
                )
            )

        for value in payments.value:
            if (
                value.stamp.connection_id != connection.id
                or value.external_invoice_id != invoice.external_id
                or value.organisation_id != invoice.organisation_id
            ):
                raise failure()  # Never copy references from an untrusted resource/tenant envelope.
            if value.amount.currency != row.currency or value.amount.exponent != row.exponent:
                raise rejected(value, "payment_currency_mismatch")
            if (
                not value.verified
                or value.stamp.observed_at > self.clock.now()
                or value.stamp.source_at is None
                or value.stamp.revision is None
            ):
                raise rejected(value, "payment_unverified")
            parent = None
            if value.parent_payment_id:
                parent = await self.db.scalar(
                    select(VerifiedPaymentAllocation).where(
                        VerifiedPaymentAllocation.tenant_id == ctx.tenant_id,
                        VerifiedPaymentAllocation.provider_connection_id == connection.id,
                        VerifiedPaymentAllocation.external_payment_id == value.parent_payment_id,
                        VerifiedPaymentAllocation.external_allocation_id == value.parent_allocation_id,
                    )
                )
                if (
                    parent is None
                    or parent.invoice_id != row.id
                    or parent.currency != value.amount.currency
                    or parent.kind != "cash"
                ):
                    raise rejected(value, "payment_parent_unresolved")
            fields = dict(
                tenant_id=ctx.tenant_id,
                provider_connection_id=connection.id,
                invoice_id=row.id,
                external_payment_id=value.external_payment_id,
                external_allocation_id=value.external_allocation_id,
                kind=value.kind,
                minor_units=value.amount.minor_units,
                currency=value.amount.currency,
                exponent=value.amount.exponent,
                parent_id=parent.id if parent else None,
                source_revision=value.stamp.revision,
                effective_at=value.stamp.source_at,
                observed_at=value.stamp.observed_at,
                organisation_id=value.organisation_id,
                synthetic=value.stamp.synthetic,
            )
            allocation = await self.db.scalar(
                select(VerifiedPaymentAllocation).where(
                    VerifiedPaymentAllocation.tenant_id == ctx.tenant_id,
                    VerifiedPaymentAllocation.provider_connection_id == connection.id,
                    VerifiedPaymentAllocation.external_payment_id == value.external_payment_id,
                    VerifiedPaymentAllocation.external_allocation_id == value.external_allocation_id,
                )
            )
            if allocation is None:
                self.db.add(VerifiedPaymentAllocation(**fields))
                await self.db.flush()
            elif any(getattr(allocation, k) != v for k, v in fields.items() if k != "observed_at"):
                raise rejected(value, "payment_identity_conflict")
        collection = await self.db.scalar(
            select(CollectionRecord)
            .where(CollectionRecord.tenant_id == ctx.tenant_id, CollectionRecord.invoice_id == row.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        # Only the genuine first invoice import bootstraps operational state. Missing
        # existing state must remain fail-closed; a financial read cannot reconstruct holds.
        if collection is None and new_snapshot:
            collection = CollectionRecord(tenant_id=ctx.tenant_id, invoice_id=row.id, hold_reasons=[])
            self.db.add(collection)
        if collection is not None:
            holds = set(collection.hold_reasons) - {"stale_accounting"}
            if not fresh:
                holds.add("stale_accounting")
            lifecycle = "closed" if fresh and invoice.status == "paid" else "active"
            if collection.id is not None and (
                collection.hold_reasons != sorted(holds) or collection.lifecycle != lifecycle
            ):
                collection.version += 1
            collection.hold_reasons = sorted(holds)
            collection.lifecycle = lifecycle
        provenance = {
            "invoice": source.model_dump(mode="json", exclude={"value"}),
            "payments": payments.model_dump(mode="json", exclude={"value"}),
        }
        observation_id = uuid4()
        self.db.add(
            AccountingObservation(
                id=observation_id,
                actor_id=ctx.actor_id,
                tenant_id=ctx.tenant_id,
                provider_connection_id=connection.id,
                invoice_id=row.id,
                external_event_id=event.external_event_id if event else None,
                invoice_data=payload,
                payment_data=[value.model_dump(mode="json") for value in payments.value],
                execution_binding=execution,
                provenance=provenance,
                observed_at=source.observed_at,
            )
        )
        self.last_observation_id = observation_id
        await self.db.flush()
        if fresh or not isinstance(ctx, ServiceContext):
            from aco.features.accounting.reconcile import SourceReadContext
            from aco.worker.followups import reconcile_source_followups

            continuation = (
                SourceReadContext(**ctx.model_dump(), observation_id=observation_id)
                if isinstance(ctx, ServiceContext)
                else ctx
            )
            await self.db.run_sync(
                lambda store: reconcile_source_followups(
                    store, continuation, observation_id, clock=self.clock
                )
            )
        read_origin = event is None or previous is not None
        if read_origin and not new_revision:
            await self.db.flush()
            return ReconciliationResult.model_validate_json(json.dumps(previous.result)) if previous else None
        event_id = uuid4()
        result = ReconciliationResult(
            event_id=event_id,
            invoice_id=row.id,
            financial_status=row.financial_status,
            collection_lifecycle=collection.lifecycle if collection is not None else "unavailable",
            source_revision=row.source_revision,
            synthetic=row.synthetic,
        )
        envelope = EventEnvelope(
            id=event_id,
            kind="AccountingDataChanged",
            tenant_id=ctx.tenant_id,
            source_key=(
                f"{connection.id}:{event.external_event_id}"
                if event is not None and not read_origin
                else json.dumps(
                    [
                        "accounting-revision",
                        str(ctx.tenant_id),
                        str(connection.id),
                        str(row.id),
                        row.source_revision,
                    ]
                )
            ),
            occurred_at=invoice.stamp.source_at,
            correlation_id=ctx.correlation_id,
            payload_version=1,
            payload=(
                event.model_dump(mode="json")
                if event is not None and not read_origin
                else {
                    "origin": "AUTHORITATIVE_READ",
                    "provider_event_id": None,
                    "invoice_id": str(row.id),
                    "external_invoice_id": invoice.external_id,
                    "source_revision": row.source_revision,
                    "observation_id": str(observation_id),
                    "trigger_event_id": str(previous.id) if previous else None,
                }
            ),
        )
        self.db.add(
            AccountingEvent(
                id=event_id,
                tenant_id=ctx.tenant_id,
                provider_connection_id=connection.id,
                external_event_id=event.external_event_id if event is not None and not read_origin else None,
                origin="AUTHORITATIVE_READ" if read_origin else "SOURCE_HINT",
                invoice_id=row.id,
                source_revision=row.source_revision,
                envelope=envelope.model_dump(mode="json"),
                provenance=provenance,
                result=result.model_dump(mode="json"),
            )
        )
        await self.db.run_sync(
            lambda store: IdentityService(store).audit(
                ctx,
                "accounting.reconciled",
                row.id,
                {
                    "source_event_id": str(event_id),
                    "source_revision": row.source_revision,
                    "synthetic": row.synthetic,
                },
            )
        )
        await self.db.flush()
        if previous is not None:
            return ReconciliationResult.model_validate_json(json.dumps(previous.result))
        return result if event is not None else None

    async def sync_accounting(self, ctx: ActorContext, cursor: SyncCursor | None) -> SyncResult:
        if isinstance(ctx, ServiceContext):
            raise failure()
        await self.authorize(ctx)
        feed = await self.adapter.get_change_feed(ctx, cursor)
        connection = await self.validate_result(ctx, feed)
        refreshed = set()
        for event in feed.value:
            await self.apply_accounting_change(ctx, event)
            refreshed.add(event.external_invoice_id)
        # Count source invoices through the full page contract; no invented count.
        count, page, seen = 0, None, set()
        while True:
            invoices = await self.adapter.list_invoices(ctx, page)
            await self.validate_result(ctx, invoices)
            count += len(invoices.value)
            for invoice in invoices.value:
                if invoice.external_id not in refreshed:
                    await self._reconcile(ctx, invoice.external_id, None)
                    refreshed.add(invoice.external_id)
            page = invoices.next_cursor
            if page is None:
                break
            if page.token in seen:
                raise failure()
            seen.add(page.token)
        connection.sync_cursor = feed.next_cursor.token if feed.next_cursor else None
        await self.db.flush()
        return SyncResult(invoice_count=count, source_watermark=feed.next_cursor)


async def reconcile_batch(
    db: AsyncSession,
    subject: AuthenticatedSubject,
    events: list[AccountingDataChanged],
    factory: Callable[[AsyncSession, UUID], AccountingPort],
) -> list[ReconciliationResult]:
    results = []
    # Caller owns one transaction. Never catch/commit partial batch successes.
    for event in events:
        connection = await db.get(ProviderConnection, event.provider_connection_id)
        if connection is None or connection.external_account_id != event.organisation_id:
            raise failure()
        ctx = await db.run_sync(
            lambda store: IdentityService(store).resolve_context(subject, connection.tenant_id)  # noqa: B023
        )
        results.append(
            await AccountingService(db, factory(db, connection.id)).apply_accounting_change(ctx, event)
        )
    return results
