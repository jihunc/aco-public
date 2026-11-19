"""Exact arithmetic boundary consumers; versioned DB flows have separate real-source tests.

Only the enumerated arithmetic cases are completed here. Full T24's exhaustive
finance consumer gate remains open for the separately identified persistence/API cases.
"""

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from boundary_evidence import BoundaryObserver

from aco.features.impact.cohorts import AllocationFact, CohortHistory, calculate_member, horizon

CASES = json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]
ARITHMETIC_IDS = {
    "cohort_includes_D_excludes_H",
    "cohort_partial_prepayment_reduces_opening",
    "cohort_fully_prepaid_excluded",
    "cohort_negative_exposure_excluded",
    "cohort_post_opening_credit_not_cash",
    "cohort_writeoff_not_cash",
    "cohort_overpayment_capped",
    "cohort_unallocated_refund_unavailable",
    "cohort_immature_not_complete",
    "cohort_missing_opening_history_not_original_amount",
    "cohort_dst_30_calendar_days",
    "days_to_collect_uses_final_cash_payment",
    "cohort_pre_D_cash_refund_within_horizon",
    "cohort_pre_D_cash_reversal_within_horizon",
    "cohort_pre_D_effective_refund_not_deducted_twice",
    "cohort_pre_D_cash_refund_missing_allocation_unavailable",
    "cohort_pre_D_cash_refund_inconsistent_opening_unavailable",
    "cohort_pre_D_cash_reversal_preserves_negative_raw_net",
}
FIXTURES = [row for row in CASES if row["id"] in ARITHMETIC_IDS]


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def evidence(data: dict[str, Any]) -> CohortHistory:
    due = date.fromisoformat(data.get("due_date", data.get("due_at", "2026-08-01")[:10]))
    timezone = data.get("timezone", "UTC")
    start, end = horizon(due, timezone)
    facts: list[AllocationFact] = []

    def add(
        key: str,
        kind: str,
        amount: int,
        at: datetime,
        observed: datetime | None = None,
        parent: str | None = None,
    ) -> None:
        facts.append(
            AllocationFact.model_validate(
                {
                    "id": key,
                    "kind": kind,
                    "minor_units": amount,
                    "effective_at": at,
                    "observed_at": observed or at,
                    "parent_id": parent,
                }
            )
        )

    if data.get("pre_D_cash_minor_units"):
        add("pre-D", "cash", data["pre_D_cash_minor_units"], start - timedelta(days=1))
    for group in ("allocations", "pre_D_allocations", "horizon_allocations"):
        for index, row in enumerate(data.get(group, [])):
            at = (
                instant(row["effective_at"] if "effective_at" in row else row["at"])
                if "at" in row or "effective_at" in row
                else start + timedelta(days=1)
            )
            add(row.get("id", f"{group}-{index}"), "cash", row["minor_units"], at)
    if "horizon_cash_minor_units" in data and data["horizon_cash_minor_units"]:
        add("horizon", "cash", data["horizon_cash_minor_units"], start + timedelta(days=1))
    for kind, name in (("credit", "post_D_credit_minor_units"), ("write_off", "post_D_writeoff_minor_units")):
        if data.get(name):
            add(name, kind, data[name], start + timedelta(days=1))
    for index, row in enumerate(data.get("adjustments", [])):
        at = instant(row["effective_at"])
        add(
            row.get("id", f"adjustment-{index}"),
            row["kind"],
            -row["minor_units"],
            at,
            instant(row["observed_at"]) if "observed_at" in row else at,
            row["allocation"],
        )
    if data.get("refund_minor_units"):
        add("unallocated-refund", "refund", -data["refund_minor_units"], start + timedelta(days=1))
    original = data["original_minor_units"]
    opening = data.get(
        "opening_exposure_minor_units",
        original - sum(row.minor_units for row in facts if row.effective_at < start),
    )
    return CohortHistory(
        invoice_id=uuid4(),
        original_due_date=due,
        issue_date=date.fromisoformat(data["issue_at"][:10])
        if "issue_at" in data
        else due - timedelta(days=30),
        timezone=timezone,
        original_minor=original,
        opening_minor=opening,
        currency=data.get("currency", "AUD"),
        exponent=2,
        outstanding_before_h=data.get(
            "source_outstanding_before_H_minor_units", 0 if "issue_at" in data else None
        ),
        outstanding_as_of=data.get("source_outstanding_as_of_minor_units", 0 if "issue_at" in data else None),
        complete=data.get("history_complete", True),
        source_watermark="explicit-synthetic-normative-fixture",
        as_of_at=instant(data["as_of"]) if "as_of" in data else end + timedelta(days=1),
        allocations=facts,
    )


@pytest.mark.parametrize("fixture", FIXTURES, ids=[row["id"] for row in FIXTURES])
def test_boundary_financial_source_events(
    fixture: dict[str, Any], boundary_observer: BoundaryObserver
) -> None:
    data, expected = fixture["input"], fixture["expected"]
    if fixture["id"] == "cohort_dst_30_calendar_days":
        start, end = horizon(date.fromisoformat(data["due_date"]), data["timezone"])
        actual: dict[str, object] = {
            "D": start.isoformat().replace("+00:00", "Z"),
            "H": end.isoformat().replace("+00:00", "Z"),
            "elapsed_hours": int((end - start).total_seconds() / 3600),
        }
    else:
        source = evidence(data)
        result = calculate_member(source)
        values: dict[str, object] = {
            "D": result.start_at.isoformat().replace("+00:00", "Z"),
            "H": result.horizon_at.isoformat().replace("+00:00", "Z"),
            "raw_net_cohort_cash_minor_units": result.raw_net_minor,
            "uncapped_net_minor_units": result.raw_net_minor,
            "recovery_numerator_minor_units": result.numerator_minor,
            "recovery_denominator_minor_units": result.opening_minor,
            "paid_in_full_count": int(result.paid_in_full) if result.paid_in_full is not None else None,
            "recovery_percent": format(result.recovery_percent.normalize(), "f")
            if result.recovery_percent is not None
            else None,
            "paid_in_full_value": result.paid_in_full,
            "value": result.recovery_percent,
            "eligible": result.eligible,
            "exclusion_reason": result.status,
            "status": result.status,
            "label": result.status,
            "credit_minor_units": result.credit_minor,
            "mature": result.mature,
            "primary_comparison_allowed": result.status == "COMPLETE" and result.mature,
            "fallback_to_original_amount": result.opening_minor is not None,
            "invented_allocation": any(
                key not in {row.id for row in source.allocations} for key in result.allocation_ids
            ),
            "guessed_exposure": result.recovery_percent is not None,
            "membership_and_denominator_preserved": result.opening_minor == source.opening_minor,
            "refund_deducted_in_numerator_minor_units": result.refunds_minor,
            "days_to_collect": format(result.days_to_collect.normalize(), "f")
            if result.days_to_collect is not None
            else None,
            "days_beyond_terms": format(result.days_beyond_terms.normalize(), "f")
            if result.days_beyond_terms is not None
            else None,
        }
        # Every expected key in each selected fixture is asserted; no fixture-key omissions.
        actual = {key: values[key] for key in expected}
    assert actual == expected
    boundary_observer.assert_observed(fixture["id"], actual)
    if "recovery_percent" in actual and actual["recovery_percent"] is not None:
        assert Decimal(str(actual["recovery_percent"])) <= 100
