"""Cash-cohort arithmetic uses exact historical evidence, never current-balance substitutes."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from aco.features.impact.cohorts import AllocationFact, CohortHistory, calculate_member, horizon


def history(**changes: object) -> CohortHistory:
    values: dict[str, object] = {
        "invoice_id": uuid4(),
        "original_due_date": date(2026, 8, 1),
        "issue_date": date(2026, 7, 1),
        "timezone": "Australia/Melbourne",
        "original_minor": 10000,
        "opening_minor": 8000,
        "currency": "AUD",
        "exponent": 2,
        "outstanding_before_h": 0,
        "outstanding_as_of": 0,
        "complete": True,
        "source_watermark": "synthetic-source-v1",
        "as_of_at": datetime(2026, 9, 1, tzinfo=UTC),
        "allocations": [
            AllocationFact(
                id="pre",
                kind="cash",
                minor_units=2000,
                effective_at=datetime(2026, 7, 25, tzinfo=UTC),
                observed_at=datetime(2026, 7, 25, tzinfo=UTC),
            ),
            AllocationFact(
                id="cash",
                kind="cash",
                minor_units=8000,
                effective_at=datetime(2026, 8, 15, tzinfo=UTC),
                observed_at=datetime(2026, 8, 15, tzinfo=UTC),
            ),
        ],
    }
    return CohortHistory.model_validate(values | changes)


def test_pre_due_refund_reduces_frozen_exposure_cash_without_moving_denominator() -> None:
    original = history()
    good = calculate_member(original)
    assert good.recovery_percent == Decimal(100) and good.paid_in_full is True
    refund = AllocationFact(
        id="refund",
        kind="refund",
        minor_units=-2000,
        parent_id="pre",
        effective_at=datetime(2026, 8, 31, tzinfo=UTC),
        observed_at=datetime(2026, 8, 31, tzinfo=UTC),
    )
    changed = history(allocations=[*original.allocations, refund], outstanding_as_of=2000)
    result = calculate_member(changed)
    assert result.opening_minor == 8000 and result.raw_net_minor == 6000
    assert result.recovery_percent == Decimal(75) and result.paid_in_full is False
    assert result.adjustment_ids == ["refund"] and result.late_adjustment_ids == ["refund"]
    assert good.recovery_percent == Decimal(100)  # Prior immutable result remains distinct.


def test_exact_horizon_dst_missing_history_and_asof_filter() -> None:
    start, end = horizon(date(2026, 10, 1), "Australia/Melbourne")
    assert start == datetime(2026, 9, 30, 14, tzinfo=UTC)
    assert end == datetime(2026, 10, 30, 13, tzinfo=UTC)
    assert (end - start).total_seconds() / 3600 == 719
    missing = calculate_member(history(opening_minor=None, complete=False))
    assert missing.recovery_percent is None and missing.status == "INCOMPLETE"
    assert missing.opening_minor is None
    immature = calculate_member(history(as_of_at=datetime(2026, 8, 1, tzinfo=UTC)))
    assert immature.status == "INCOMPLETE" and not immature.mature
    source = history()
    future_refund = AllocationFact(
        id="future",
        kind="refund",
        minor_units=-2000,
        parent_id="pre",
        effective_at=datetime(2026, 8, 31, tzinfo=UTC),
        observed_at=datetime(2026, 9, 2, tzinfo=UTC),
    )
    result = calculate_member(history(allocations=[*source.allocations, future_refund]))
    assert result.recovery_percent == Decimal(100) and result.adjustment_ids == []
    start, end = horizon(source.original_due_date, source.timezone)
    facts = [
        AllocationFact(id=str(i), kind="cash", minor_units=n, effective_at=at, observed_at=at)
        for i, (at, n) in enumerate([(start, 2500), (end - timedelta(seconds=1), 2500), (end, 5000)])
    ]
    result = calculate_member(history(opening_minor=10000, allocations=facts, outstanding_before_h=5000))
    assert result.raw_net_minor == 5000 and result.paid_in_full is False


def test_negative_cash_noncash_missing_parent_and_inconsistent_opening_fail_honestly() -> None:
    source = history()
    bad = AllocationFact(
        id="bad",
        kind="reversal",
        minor_units=-2000,
        parent_id="missing",
        effective_at=datetime(2026, 8, 15, tzinfo=UTC),
        observed_at=datetime(2026, 8, 15, tzinfo=UTC),
    )
    assert calculate_member(history(allocations=[*source.allocations, bad])).status == "allocation_review"
    assert calculate_member(history(opening_minor=7000)).status == "opening_history_review"
    credit = AllocationFact(
        id="credit",
        kind="credit",
        minor_units=8000,
        effective_at=datetime(2026, 8, 15, tzinfo=UTC),
        observed_at=datetime(2026, 8, 15, tzinfo=UTC),
    )
    result = calculate_member(history(allocations=[source.allocations[0], credit]))
    assert result.recovery_percent == Decimal(0) and result.paid_in_full is False
    assert result.days_to_collect is None


def test_incomplete_refresh_preserves_known_frozen_eligibility_and_denominator() -> None:
    result = calculate_member(history(complete=False))
    assert result.status == "INCOMPLETE" and result.recovery_percent is None
    assert result.eligible is True and result.opening_minor == 8000
