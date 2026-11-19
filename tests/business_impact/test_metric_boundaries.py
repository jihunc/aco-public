"""Canonical null-ratio and no-FX arithmetic, with all normative outputs observed."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from boundary_evidence import BoundaryObserver

from aco.features.impact.cohorts import percent
from aco.features.impact.economics import estimate_capacity
from aco.features.impact.financial_metrics import CashFact, InvoiceBasis, known_cash

CASES = json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]
FIXTURES = [
    row for row in CASES if row["id"] in {"metric_zero_denominator_null", "metric_currency_mix_no_FX"}
]


@pytest.mark.parametrize("fixture", FIXTURES, ids=[row["id"] for row in FIXTURES])
def test_boundary_unavailable_and_currency_separation(
    fixture: dict[str, Any],
    boundary_observer: BoundaryObserver,
) -> None:
    data = fixture["input"]
    if "numerator" in data:
        numerator, denominator = Decimal(data["numerator"]), Decimal(data["denominator"])
        result = estimate_capacity(
            eligible=1,
            baseline=numerator,
            human_minutes=Decimal(0),
            wage=Decimal(60),
            provider=denominator,
            fixed=Decimal(0),
            approved=True,
            same_currency=True,
            human_time_measured=True,
        )
        assert percent(int(numerator), int(denominator)) == result.estimated_roi_percent
        actual: dict[str, object] = {"value": result.estimated_roi_percent, "label": result.label}
    else:
        tenant, connection = uuid4(), uuid4()
        stamp = datetime(2026, 9, 1, tzinfo=UTC)
        facts, invoices = [], {}
        for item in data["amounts"]:
            invoice_id, allocation_id = uuid4(), uuid4()
            invoices[invoice_id] = InvoiceBasis(
                invoice_id,
                tenant,
                connection,
                "NORMATIVE-SYNTHETIC",
                item["currency"],
                2,
                True,
                "fresh",
                stamp,
            )
            facts.append(
                CashFact(
                    allocation_id,
                    tenant,
                    connection,
                    invoice_id,
                    "NORMATIVE-SYNTHETIC",
                    item["currency"],
                    2,
                    True,
                    "cash",
                    int(Decimal(item["amount"]) * 100),
                    None,
                    stamp,
                    stamp,
                    stamp,
                    "1",
                )
            )
        groups = known_cash(tenant, stamp, stamp + timedelta(seconds=1), stamp, facts, invoices)
        assert len(groups) == len(data["amounts"])
        amounts = [
            Decimal(group.net_cash.minor_units) / 100 for group in groups if group.net_cash is not None
        ]
        assert len(amounts) == 2
        # Canonical economics refuses to combine unlike currency components without approved FX.
        result = estimate_capacity(
            eligible=1,
            baseline=Decimal(1),
            human_minutes=Decimal(0),
            wage=Decimal(60),
            provider=amounts[0],
            fixed=amounts[1],
            approved=True,
            same_currency=len({group.currency for group in groups}) == 1,
            human_time_measured=True,
        )
        assert data["approved_fx_policy"] is None
        actual = {
            "combined_value": result.incremental_cost_denominator,
            "separate_currencies": [group.currency for group in groups],
        }
    assert actual == fixture["expected"]
    boundary_observer.assert_observed(fixture["id"], actual)
