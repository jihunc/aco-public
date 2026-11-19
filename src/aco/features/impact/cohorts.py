"""aco_cash_v1 arithmetic over complete, source-verified historical inputs.

These values are internal evidence, never an HTTP assertion of financial truth.
The history adapter and persistence service establish their authority and freeze membership.
"""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, ConfigDict

from aco.core.types import StrictModel


class AllocationFact(StrictModel):
    id: str
    kind: Literal["cash", "refund", "reversal", "credit", "write_off"]
    minor_units: int
    effective_at: AwareDatetime
    observed_at: AwareDatetime
    parent_id: str | None = None


class CohortHistory(StrictModel):
    invoice_id: UUID
    original_due_date: date
    issue_date: date
    timezone: str
    original_minor: int
    opening_minor: int | None
    currency: str
    exponent: int
    outstanding_before_h: int | None
    outstanding_as_of: int | None
    complete: bool
    source_watermark: str
    as_of_at: AwareDatetime
    allocations: list[AllocationFact]
    opening_status: str = "open"
    source_consistent: bool = True


class MemberCalculation(StrictModel):
    model_config = ConfigDict(frozen=False)
    invoice_id: UUID
    start_at: datetime
    horizon_at: datetime
    mature: bool
    eligible: bool
    status: str
    opening_minor: int | None
    raw_net_minor: int | None = None
    numerator_minor: int | None = None
    refunds_minor: int = 0
    reversals_minor: int = 0
    credit_minor: int = 0
    recovery_percent: Decimal | None = None
    paid_in_full: bool | None = None
    days_to_collect: Decimal | None = None
    days_beyond_terms: Decimal | None = None
    adjustment_ids: list[str] = []
    late_adjustment_ids: list[str] = []
    allocation_ids: list[str] = []


def horizon(due_date: date, timezone: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone)
    return (
        datetime.combine(due_date, time.min, zone).astimezone(UTC),
        datetime.combine(due_date + timedelta(days=30), time.min, zone).astimezone(UTC),
    )


def percent(numerator: int, denominator: int) -> Decimal | None:
    return Decimal(numerator) * 100 / Decimal(denominator) if denominator > 0 else None


def calculate_member(source: CohortHistory) -> MemberCalculation:
    start, end = horizon(source.original_due_date, source.timezone)
    result = MemberCalculation(
        invoice_id=source.invoice_id,
        start_at=start,
        horizon_at=end,
        mature=end <= source.as_of_at,
        eligible=source.opening_minor is not None
        and source.opening_minor > 0
        and source.opening_status not in {"voided", "cancelled"},
        status="INCOMPLETE",
        opening_minor=source.opening_minor,
    )
    if not source.complete or source.opening_minor is None or not result.mature:
        return result
    if source.opening_minor <= 0 or source.opening_status in {"voided", "cancelled"}:
        return result.model_copy(
            update={
                "status": "nonpositive_opening_exposure" if source.opening_minor <= 0 else "void_or_cancelled"
            }
        )
    result.eligible = True
    facts = [
        row
        for row in source.allocations
        if row.observed_at <= source.as_of_at and row.effective_at <= source.as_of_at
    ]
    identities = {row.id: row for row in facts}
    if len(identities) != len(facts):
        result.status = "allocation_review"
        return result
    cash = {row.id: row for row in facts if row.kind == "cash"}
    outflows = [row for row in facts if row.kind in {"refund", "reversal"}]
    if any(row.minor_units <= 0 or row.parent_id for row in cash.values()) or any(
        row.minor_units >= 0
        or row.parent_id not in cash
        or cash[row.parent_id].effective_at > row.effective_at
        for row in outflows
    ):
        result.status = "allocation_review"
        return result
    if any(
        -sum(row.minor_units for row in outflows if row.parent_id == key) > parent.minor_units
        for key, parent in cash.items()
    ):
        result.status = "allocation_review"
        return result
    noncash = [row for row in facts if row.kind in {"credit", "write_off"}]
    reconstructed = source.original_minor - sum(row.minor_units for row in facts if row.effective_at < start)
    if not source.source_consistent or reconstructed != source.opening_minor:
        result.status = "opening_history_review"
        return result
    # Both horizon cash and exposure-reducing pre-D receipts remain refund targets.
    qualifying = {key for key, row in cash.items() if row.effective_at < end}
    adjustments = [row for row in outflows if row.effective_at >= start and row.parent_id in qualifying]
    raw = sum(row.minor_units for row in cash.values() if start <= row.effective_at < end)
    raw += sum(row.minor_units for row in adjustments)
    result.raw_net_minor = raw
    result.numerator_minor = max(0, min(raw, source.opening_minor))
    result.recovery_percent = percent(result.numerator_minor, source.opening_minor)
    result.refunds_minor = -sum(row.minor_units for row in adjustments if row.kind == "refund")
    result.reversals_minor = -sum(row.minor_units for row in adjustments if row.kind == "reversal")
    result.credit_minor = sum(
        row.minor_units for row in noncash if row.kind == "credit" and row.effective_at >= start
    )
    result.adjustment_ids = sorted(row.id for row in adjustments)
    result.late_adjustment_ids = sorted(
        row.id for row in adjustments if row.effective_at >= end or row.observed_at >= end
    )
    result.allocation_ids = sorted(identities)
    result.status = "COMPLETE"
    if source.outstanding_before_h is not None and source.outstanding_as_of is not None:
        result.paid_in_full = (
            raw >= source.opening_minor
            and source.outstanding_before_h == 0
            and source.outstanding_as_of == 0
            and not any(row.effective_at >= start and row.minor_units != 0 for row in noncash)
        )
    # BI04 is full original cash settlement, not just settlement of the opening exposure.
    if (
        source.original_minor > 0
        and source.outstanding_as_of == 0
        and not noncash
        and sum(row.minor_units for row in [*cash.values(), *outflows]) >= source.original_minor
    ):
        running, settled = 0, None
        for row in sorted([*cash.values(), *outflows], key=lambda value: (value.effective_at, value.id)):
            prior = running
            running += row.minor_units
            if prior < source.original_minor <= running:
                settled = row.effective_at
            if running < source.original_minor:
                settled = None
        if settled is not None:
            issue = datetime.combine(source.issue_date, time.min, ZoneInfo(source.timezone)).astimezone(UTC)
            result.days_to_collect = Decimal(str((settled - issue).total_seconds())) / 86400
            result.days_beyond_terms = Decimal(str((settled - start).total_seconds())) / 86400
    return result
