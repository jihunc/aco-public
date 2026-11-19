"""Validated canonical accounting data; no financial authority from untrusted hints."""

from datetime import date
from typing import Literal
from uuid import UUID

from iso4217 import Currency
from pydantic import AwareDatetime, Field, field_validator, model_validator

from aco.core.types import Money, ProviderCost, SourceStamp, StrictModel


def validate_currency(value: Money) -> Money:
    if Currency(value.currency).exponent != value.exponent:
        raise ValueError("Currency exponent does not match ISO 4217")
    return value


class SyncCursor(StrictModel):
    token: str


class AccountingDataChanged(StrictModel):
    provider_connection_id: UUID
    organisation_id: str
    external_invoice_id: str
    external_event_id: str


class CustomerData(StrictModel):
    external_id: str
    name: str
    email: str | None
    organisation_id: str
    stamp: SourceStamp


class InvoiceData(StrictModel):
    external_id: str
    external_customer_id: str
    number: str
    issue_date: date
    due_date: date
    original: Money
    outstanding: Money
    status: Literal["open", "partially_paid", "paid", "voided", "credited", "written_off", "unknown"]
    source_status: str
    organisation_id: str
    stamp: SourceStamp
    verified: bool

    @field_validator("original", "outstanding")
    @classmethod
    def currency(cls, value: Money) -> Money:
        return validate_currency(value)

    @model_validator(mode="after")
    def coherent(self) -> "InvoiceData":
        if (self.original.currency, self.original.exponent) != (
            self.outstanding.currency,
            self.outstanding.exponent,
        ):
            raise ValueError("No implicit FX")
        if self.status == "paid" and self.outstanding.minor_units != 0:
            raise ValueError("Paid source requires zero outstanding")
        return self


class PaymentData(StrictModel):
    external_payment_id: str
    external_allocation_id: str
    external_invoice_id: str
    kind: Literal["cash", "refund", "reversal", "credit", "write_off"]
    amount: Money
    parent_payment_id: str | None
    parent_allocation_id: str | None
    organisation_id: str
    stamp: SourceStamp
    verified: bool

    @field_validator("amount")
    @classmethod
    def currency(cls, value: Money) -> Money:
        return validate_currency(value)

    @model_validator(mode="after")
    def signed_fact(self) -> "PaymentData":
        if self.kind == "cash" and self.amount.minor_units <= 0:
            raise ValueError("Cash receipt must be positive")
        if self.kind in {"refund", "reversal"} and (
            self.amount.minor_units >= 0 or not self.parent_payment_id or not self.parent_allocation_id
        ):
            raise ValueError("Refund/reversal needs negative amount and original allocation")
        if bool(self.parent_payment_id) != bool(self.parent_allocation_id):
            raise ValueError("Incomplete parent identity")
        return self


class AccountingResult[T](StrictModel):
    value: T
    tenant_id: UUID
    provider_connection_id: UUID
    organisation_id: str
    provider: str
    native_operation_id: str
    correlation_id: UUID
    observed_at: AwareDatetime
    currentness: Literal["CURRENT", "STALE", "UNKNOWN"] = "UNKNOWN"
    authenticity: Literal["VERIFIED", "UNKNOWN"]
    attempt: int = Field(default=1, ge=1)
    cost: ProviderCost
    next_cursor: SyncCursor | None = None


class DemoPaymentCommand(StrictModel):
    provider_connection_id: UUID
    external_invoice_id: str
    amount: Money
    operation_id: UUID

    @field_validator("amount")
    @classmethod
    def positive_cash(cls, value: Money) -> Money:
        validate_currency(value)
        if value.minor_units <= 0:
            raise ValueError("Demo receipt must be positive")
        return value


class ReconciliationResult(StrictModel):
    event_id: UUID
    invoice_id: UUID
    financial_status: str
    collection_lifecycle: str
    source_revision: str
    synthetic: bool


EventRef = ReconciliationResult


class SyncResult(StrictModel):
    invoice_count: int
    source_watermark: SyncCursor | None
