"""Independent document-source shape for contracts; never a selectable runtime fallback."""

from collections.abc import Mapping
from datetime import datetime
from typing import TypeVar, cast
from uuid import UUID, uuid4

from aco.core.context import ActorContext
from aco.core.types import (
    CostQuality,
    Money,
    ProviderCapabilities,
    ProviderCost,
    ProviderErrorCode,
    ProviderFailure,
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
from aco.features.identity.models import now
from aco.integrations.ports.accounting import AccountingFailure

T = TypeVar("T")


class DocumentAccounting:
    pricing_currency = "AUD"

    def __init__(self, tenant_id: UUID, connection_id: UUID, organisation: str, observed_at: datetime):
        self.tenant_id, self.connection_id, self.organisation, self.observed_at = (
            tenant_id,
            connection_id,
            organisation,
            observed_at,
        )
        self.effective_at = observed_at
        self.invoice_effective_at = observed_at
        self.native_current: bool | None = True
        self.verified = True
        self.read_at: datetime | None = None
        self.payment_observed_at: datetime | None = None
        # Independent native shape: no canonical DTO serialization or Demo adapter inheritance.
        self.documents = [
            {
                "reference": "DOC-1",
                "account": "BUYER",
                "issued": "2026-10-01",
                "due": "2026-11-01",
                "units": 10000,
                "unpaid": 7500,
                "ccy": "AUD",
                "state": "PARTIAL",
                "etag": "v1",
            }
        ]
        self.receipts = [("RCPT-1", "split-9", "DOC-1", 2500)]
        self.native_failure: str | None = None
        self.calls: list[str] = []

    def gate(self, ctx: ActorContext) -> None:
        if ctx.tenant_id != self.tenant_id:
            raise AccountingFailure(
                ProviderFailure(
                    code=ProviderErrorCode.VALIDATION_FAILED, retryable=False, may_have_side_effect=False
                )
            )
        codes = {
            "expired": ProviderErrorCode.AUTH_EXPIRED,
            "busy": ProviderErrorCode.RATE_LIMITED,
            "offline": ProviderErrorCode.TEMPORARILY_UNAVAILABLE,
            "bad": ProviderErrorCode.VALIDATION_FAILED,
        }
        if self.native_failure:
            from datetime import timedelta

            code = codes[self.native_failure]
            raise AccountingFailure(
                ProviderFailure(
                    code=code,
                    retryable=code
                    in {ProviderErrorCode.RATE_LIMITED, ProviderErrorCode.TEMPORARILY_UNAVAILABLE},
                    may_have_side_effect=False,
                    request_id="document-request",
                    retry_after=timedelta(seconds=7) if code == ProviderErrorCode.RATE_LIMITED else None,
                )
            )

    def result(self, ctx: ActorContext, value: T, cursor: SyncCursor | None = None) -> AccountingResult[T]:
        self.gate(ctx)
        return AccountingResult(
            value=value,
            tenant_id=self.tenant_id,
            provider_connection_id=self.connection_id,
            organisation_id=self.organisation,
            provider="document-test",
            native_operation_id=f"doc:{uuid4()}",
            correlation_id=ctx.correlation_id,
            observed_at=self.read_at or now(),
            currentness="CURRENT"
            if self.native_current
            else "STALE"
            if self.native_current is False
            else "UNKNOWN",
            authenticity="VERIFIED",
            cost=ProviderCost(amount=None, currency="AUD", quality=CostQuality.UNKNOWN),
            next_cursor=cursor,
        )

    def stamp(self, key: str, revision: str = "v1") -> SourceStamp:
        return SourceStamp(
            connection_id=self.connection_id,
            external_id=key,
            observed_at=self.payment_observed_at
            if key != "DOC-1" and self.payment_observed_at
            else self.observed_at,
            source_at=self.invoice_effective_at if key == "DOC-1" else self.effective_at,
            revision=revision,
            synthetic=True,
        )

    async def describe_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            features=frozenset(
                {
                    "list_customers",
                    "list_invoices",
                    "get_invoice",
                    "list_payments",
                    "get_change_feed",
                    "synthetic",
                    "pagination",
                }
            ),
            limits={"page_size": 1},
        )

    async def verify_webhook(
        self, raw_body: bytes, headers: Mapping[str, str]
    ) -> list[AccountingDataChanged]:
        raise AccountingFailure(
            ProviderFailure(
                code=ProviderErrorCode.UNSUPPORTED_CAPABILITY, retryable=False, may_have_side_effect=False
            )
        )

    async def list_customers(
        self, ctx: ActorContext, cursor: SyncCursor | None = None
    ) -> AccountingResult[list[CustomerData]]:
        self.calls.append("customers")
        if cursor is not None:
            self.gate(ctx)
            raise AccountingFailure(
                ProviderFailure(
                    code=ProviderErrorCode.VALIDATION_FAILED, retryable=False, may_have_side_effect=False
                )
            )
        return self.result(
            ctx,
            [
                CustomerData(
                    external_id="BUYER",
                    name="Synthetic document buyer",
                    email="buyer@example.test",
                    organisation_id=self.organisation,
                    stamp=self.stamp("BUYER"),
                )
            ],
        )

    async def list_invoices(
        self, ctx: ActorContext, cursor: SyncCursor | None = None
    ) -> AccountingResult[list[InvoiceData]]:
        from datetime import date

        self.calls.append("invoices")
        self.gate(ctx)
        if cursor is not None:
            if cursor.token != f"{self.tenant_id}:{self.connection_id}:end":
                raise AccountingFailure(
                    ProviderFailure(
                        code=ProviderErrorCode.VALIDATION_FAILED, retryable=False, may_have_side_effect=False
                    )
                )
            return self.result(ctx, [])
        values = [
            InvoiceData(
                external_id=str(d["reference"]),
                external_customer_id=str(d["account"]),
                number=str(d["reference"]),
                issue_date=date.fromisoformat(str(d["issued"])),
                due_date=date.fromisoformat(str(d["due"])),
                original=Money(minor_units=cast(int, d["units"]), currency=str(d["ccy"]), exponent=2),
                outstanding=Money(minor_units=cast(int, d["unpaid"]), currency=str(d["ccy"]), exponent=2),
                status="partially_paid"
                if d["state"] == "PARTIAL"
                else "paid"
                if d["state"] == "PAID"
                else "unknown",
                source_status=str(d["state"]),
                organisation_id=self.organisation,
                stamp=self.stamp(str(d["reference"]), str(d["etag"])),
                verified=self.verified,
            )
            for d in self.documents
        ]
        return self.result(ctx, values, SyncCursor(token=f"{self.tenant_id}:{self.connection_id}:end"))

    async def get_invoice(self, ctx: ActorContext, invoice_key: str) -> AccountingResult[InvoiceData]:
        self.calls.append("get")
        page = await self.list_invoices(ctx)
        if invoice_key != "DOC-1":
            raise AccountingFailure(
                ProviderFailure(
                    code=ProviderErrorCode.VALIDATION_FAILED, retryable=False, may_have_side_effect=False
                )
            )
        return self.result(ctx, page.value[0])

    async def list_payments(self, ctx: ActorContext, invoice_key: str) -> AccountingResult[list[PaymentData]]:
        self.calls.append("payments")
        return self.result(
            ctx,
            [
                PaymentData(
                    external_payment_id=p,
                    external_allocation_id=a,
                    external_invoice_id=i,
                    kind="cash",
                    amount=Money(minor_units=amount, currency="AUD", exponent=2),
                    parent_payment_id=None,
                    parent_allocation_id=None,
                    organisation_id=self.organisation,
                    stamp=self.stamp(f"{p}/{a}"),
                    verified=True,
                )
                for p, a, i, amount in self.receipts
                if i == invoice_key
            ],
        )

    async def get_change_feed(
        self, ctx: ActorContext, cursor: SyncCursor | None = None
    ) -> AccountingResult[list[AccountingDataChanged]]:
        self.calls.append("changes")
        if cursor is not None and cursor.token != f"{self.tenant_id}:{self.connection_id}:etag-v1":
            raise AccountingFailure(
                ProviderFailure(
                    code=ProviderErrorCode.VALIDATION_FAILED, retryable=False, may_have_side_effect=False
                )
            )
        events = (
            []
            if cursor
            else [
                AccountingDataChanged(
                    provider_connection_id=self.connection_id,
                    organisation_id=self.organisation,
                    external_invoice_id="DOC-1",
                    external_event_id="notification-v1",
                )
            ]
        )
        return self.result(ctx, events, SyncCursor(token=f"{self.tenant_id}:{self.connection_id}:etag-v1"))
