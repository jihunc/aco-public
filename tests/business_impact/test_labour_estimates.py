"""All normative ROI outputs use production arithmetic, never achieved savings."""

import json
from decimal import Decimal
from pathlib import Path

from boundary_evidence import BoundaryObserver

from aco.features.impact.economics import CapacityEstimate, estimate_capacity


def test_boundary_estimated_roi(boundary_observer: BoundaryObserver) -> None:
    cases = json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]
    tested: set[str] = set()
    for case in cases:
        if case["domain"] != "roi":
            continue
        source = case["input"]
        result = estimate_capacity(
            eligible=source["eligible_cases"],
            baseline=Decimal(source["baseline_minutes_per_issue"]),
            human_minutes=Decimal(source["measured_human_minutes"])
            if source["measured_human_minutes"] is not None
            else None,
            wage=Decimal(source["wage_per_hour"]) if "wage_per_hour" in source else None,
            provider=Decimal(source["incremental_provider_cost"]),
            fixed=Decimal(source["incremental_fixed_cost"]) if "incremental_fixed_cost" in source else None,
            approved=True,
            same_currency=True,
            human_time_measured=True,
        )
        actual = result.model_dump()
        for key, expected in case["expected"].items():
            value = actual[key]
            assert value == (Decimal(expected) if isinstance(value, Decimal) else expected), key
        boundary_observer.assert_observed(
            case["id"],
            {
                key: format(actual[key].normalize(), "f") if isinstance(actual[key], Decimal) else actual[key]
                for key in case["expected"]
            },
        )
        tested.add(case["id"])
    assert tested == {case["id"] for case in cases if case["domain"] == "roi"}


def test_negative_capacity_no_double_subtraction_and_missing_approval_fx_or_cost() -> None:
    def calculate(
        *,
        approved: bool = True,
        human_time_measured: bool = True,
        same_currency: bool = True,
        provider: Decimal | None = Decimal(10),
        fixed: Decimal | None = Decimal(20),
        human_minutes: Decimal | None = Decimal(40),
    ) -> CapacityEstimate:
        return estimate_capacity(
            eligible=1,
            baseline=Decimal(10),
            human_minutes=human_minutes,
            wage=Decimal(60),
            provider=provider,
            fixed=fixed,
            approved=approved,
            same_currency=same_currency,
            human_time_measured=human_time_measured,
        )

    negative = calculate()
    assert negative.estimated_time_saved_minutes == -30
    assert negative.estimated_net_benefit == -60
    assert negative.estimated_roi_percent == -200
    for result in (
        calculate(human_time_measured=False),
        calculate(approved=False),
        calculate(same_currency=False),
        calculate(provider=None),
        calculate(fixed=None),
        calculate(human_minutes=None),
        calculate(provider=Decimal(0), fixed=Decimal(0)),
    ):
        assert result.estimated_roi_percent is None
