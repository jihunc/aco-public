"""Exact supplied calendar fixtures through production recipient-local arithmetic."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from boundary_evidence import BoundaryObserver

CASES = [
    case
    for case in json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]
    if case["domain"] == "schedule"
]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_boundary_recipient_calendar(case: dict[str, Any], boundary_observer: BoundaryObserver) -> None:
    from aco.features.policies.schedule import recipient_calendar

    data = case["input"]
    candidate = datetime.fromisoformat(
        data.get("calendar_candidate", data.get("local_datetime", "2026-11-17T10:00:00"))
    )
    actual = recipient_calendar(
        candidate,
        data["recipient_timezone"],
        tenant_timezone=data.get("tenant_timezone"),
        owner_confirmed=data.get("owner_confirmed_recipient_uses_tenant_timezone", False),
    )
    observed = {
        "allowed": actual.allowed,
        "reason": actual.reason,
        "next_local_datetime": actual.next_local.isoformat() if actual.next_local else None,
        "next_utc": actual.next_local.astimezone(UTC).isoformat().replace("+00:00", "Z")
        if actual.next_local
        else None,
    }
    for field, value in case["expected"].items():
        assert observed[field] == value, (case["id"], field, observed)
    boundary_observer.assert_observed(case["id"], {key: observed[key] for key in case["expected"]})


def test_dst_and_quiet_hour_boundaries() -> None:
    from aco.features.policies.schedule import recipient_calendar

    decision = recipient_calendar(datetime(2026, 11, 20, 17), "Australia/Melbourne")
    assert decision.next_local is not None
    assert decision.next_local.isoformat() == "2026-11-23T09:00:00+11:00"
    decision = recipient_calendar(
        datetime(2026, 11, 17, 10), None, tenant_timezone="Australia/Melbourne", owner_confirmed=True
    )
    assert decision.allowed
    decision = recipient_calendar(datetime(2026, 11, 17, 10), "Australia/Melbourne", contradictory=True)
    assert not decision.allowed and decision.reason == "timezone_review"
