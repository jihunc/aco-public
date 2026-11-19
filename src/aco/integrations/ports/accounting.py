"""Read-only asynchronous accounting boundary. No source mutation operations."""

from collections.abc import Mapping, Sequence
from typing import Annotated, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import Field, ValidationError

from aco.core.context import ActorContext
from aco.core.types import ProviderCapabilities, ProviderErrorCode, ProviderFailure, StrictModel
from aco.features.accounting.dto import (
    AccountingDataChanged,
    AccountingResult,
    CustomerData,
    InvoiceData,
    PaymentData,
    SyncCursor,
)

NativeReference = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[^\x00-\x1f\x7f]+$")]
RejectionReason = Literal[
    "payment_shape_invalid",
    "payment_currency_mismatch",
    "payment_parent_unresolved",
    "payment_identity_conflict",
    "payment_unverified",
]


class RejectedPaymentReference(StrictModel):
    native_source_id: NativeReference
    source_revision: NativeReference | None = None
    external_payment_id: NativeReference | None = None
    external_allocation_id: NativeReference | None = None
    parent_payment_id: NativeReference | None = None
    parent_allocation_id: NativeReference | None = None
    kind: Literal["cash", "refund", "reversal", "credit", "write_off"] | None = None


class AccountingRejectionEvidence(StrictModel):
    authenticity: Literal["UNVERIFIED"] = "UNVERIFIED"
    tenant_id: UUID
    provider_connection_id: UUID
    external_invoice_id: NativeReference
    reason: RejectionReason
    candidate: RejectedPaymentReference
    related_reversal_refs: tuple[RejectedPaymentReference, ...] = Field(default=(), max_length=8)
    related_reversal_refs_truncated: bool = False


def rejection_reference(
    payload: Mapping[str, object], native_source_id: str, source_revision: str | None
) -> RejectedPaymentReference | None:
    # Whitelist references only: never retain money, private text or validation input dumps.
    fields = {
        key: payload.get(key)
        for key in (
            "external_payment_id",
            "external_allocation_id",
            "parent_payment_id",
            "parent_allocation_id",
            "kind",
        )
    }
    try:
        return RejectedPaymentReference.model_validate(
            {
                **fields,
                "native_source_id": native_source_id,
                "source_revision": source_revision,
            }
        )
    except ValidationError:
        return None  # Invalid/oversized references are not silently truncated into invented identities.


def source_rejection(
    ctx: ActorContext,
    connection_id: UUID,
    invoice_key: str,
    reason: RejectionReason,
    candidate: RejectedPaymentReference | None,
    same_scope_candidates: Sequence[RejectedPaymentReference | None] = (),
) -> AccountingRejectionEvidence | None:
    """Caller must establish actual envelope/resource scope before supplying candidate references."""
    if candidate is None:
        return None
    related = list(
        {
            ref.model_dump_json(): ref
            for ref in same_scope_candidates
            if ref is not None and ref != candidate and ref.kind in {"refund", "reversal"}
        }.values()
    )
    try:
        return AccountingRejectionEvidence(
            tenant_id=ctx.tenant_id,
            provider_connection_id=connection_id,
            external_invoice_id=invoice_key,
            reason=reason,
            candidate=candidate,
            related_reversal_refs=tuple(related[:8]),
            related_reversal_refs_truncated=len(related) > 8,
        )
    except ValidationError:
        return None


class AccountingFailure(Exception):
    def __init__(
        self, failure: ProviderFailure, *, rejection_evidence: AccountingRejectionEvidence | None = None
    ):
        self.failure = failure
        self.rejection_evidence = rejection_evidence
        super().__init__(failure.code.value)


class AccountingPort(Protocol):
    connection_id: UUID

    @property
    def pricing_currency(self) -> str | None: ...
    async def list_customers(
        self, ctx: ActorContext, cursor: SyncCursor | None = None
    ) -> AccountingResult[list[CustomerData]]: ...
    async def list_invoices(
        self, ctx: ActorContext, cursor: SyncCursor | None = None
    ) -> AccountingResult[list[InvoiceData]]: ...
    async def get_invoice(self, ctx: ActorContext, invoice_key: str) -> AccountingResult[InvoiceData]: ...
    async def list_payments(
        self, ctx: ActorContext, invoice_key: str
    ) -> AccountingResult[list[PaymentData]]: ...
    async def get_change_feed(
        self, ctx: ActorContext, cursor: SyncCursor | None = None
    ) -> AccountingResult[list[AccountingDataChanged]]: ...
    async def verify_webhook(
        self, raw_body: bytes, headers: Mapping[str, str]
    ) -> list[AccountingDataChanged]: ...
    async def describe_capabilities(self) -> ProviderCapabilities: ...


def failure(
    code: ProviderErrorCode = ProviderErrorCode.VALIDATION_FAILED,
    *,
    rejection_evidence: AccountingRejectionEvidence | None = None,
) -> AccountingFailure:
    return AccountingFailure(
        ProviderFailure(
            code=code,
            retryable=code == ProviderErrorCode.TEMPORARILY_UNAVAILABLE,
            may_have_side_effect=False,
            request_id=str(uuid4()),
        ),
        rejection_evidence=rejection_evidence,
    )
