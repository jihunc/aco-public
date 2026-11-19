"""Optional complete historical accounting evidence; current reads do not imply completeness."""

from datetime import date, datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import AwareDatetime

from aco.core.context import TenantContext
from aco.core.types import StrictModel
from aco.features.accounting.dto import AccountingResult, InvoiceData, PaymentData


class HistoricalInvoiceRevision(StrictModel):
    source_id: UUID
    effective_at: AwareDatetime
    observed_at: AwareDatetime
    invoice: InvoiceData


class HistoricalAllocation(StrictModel):
    source_id: UUID
    effective_at: AwareDatetime
    observed_at: AwareDatetime
    payment: PaymentData


class HistoricalInvoice(StrictModel):
    external_invoice_id: str
    side: Literal["BASELINE_SYNTHETIC", "AFTER_SYNTHETIC", "OBSERVED"]
    original_due_date: date
    issue_date: date
    revisions: list[HistoricalInvoiceRevision]
    allocations: list[HistoricalAllocation]
    history_complete: bool = True
    business_segment: str | None = None


class CohortSourceHistory(StrictModel):
    manifest_id: UUID | None
    timezone: str
    credit_terms_days: int
    business_segment: str
    complete_through: AwareDatetime
    source_watermark: str
    synthetic: bool = True
    invoices: list[HistoricalInvoice]


class AccountingHistoryPort(Protocol):
    connection_id: UUID

    async def read_cohort_history(self, ctx: TenantContext) -> AccountingResult[CohortSourceHistory]: ...


class ObservedAccountingHistoryPort(Protocol):
    """Optional retained history capability; missing opening coverage is explicit per member."""

    connection_id: UUID

    async def read_observed_history(
        self, ctx: TenantContext, as_of_at: datetime | None = None
    ) -> AccountingResult[CohortSourceHistory]: ...
