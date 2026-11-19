"""Normative shared-charge arithmetic; actual persistence is tested separately."""

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from boundary_evidence import BoundaryObserver

from aco.features.impact.economics import cost_components, equal_weights


def test_boundary_cost_component_allocation(boundary_observer: BoundaryObserver) -> None:
    cases = json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]
    tested: set[str] = set()
    for case in cases:
        if case["domain"] != "cost":
            continue
        inputs, expected = case["input"], case["expected"]
        actual: dict[str, object]
        if case["id"] == "cost_unknown_not_zero":
            result = cost_components(provider=None, human_minutes=None, wage=None, fixed=None, resolved=0)
            actual = {
                "cost_amount": result.provider,
                "cost_quality": result.provider_quality,
                "billable_activation_allowed": result.billable_activation_allowed,
            }
        elif case["id"] == "cost_shared_leaf_allocation_once":
            actual = shared_cost_observation(inputs)
        elif case["id"] == "cost_unknown_components_make_partial_total":
            result = cost_components(
                provider=Decimal(inputs["provider_cost"]),
                human_minutes=None,
                wage=None,
                fixed=None,
                resolved=inputs["resolved_cases"],
            )
            actual = {
                "complete_cost_per_resolution": result.complete_cost_per_resolution,
                "known_provider_component": str(result.provider),
                "status": result.status,
            }
        else:
            pytest.fail(f"Unconsumed normative cost case: {case['id']}")
        assert actual == expected
        boundary_observer.assert_observed(case["id"], actual)
        tested.add(case["id"])
    assert tested == {case["id"] for case in cases if case["domain"] == "cost"}


def test_equal_weights_exact_residual_and_invalid_population() -> None:
    assert sum(equal_weights(7)) == Decimal(1)
    with pytest.raises(ValueError):
        equal_weights(0)
    result = cost_components(
        provider=Decimal(3), human_minutes=Decimal(60), wage=Decimal(6), fixed=Decimal(1), resolved=2
    )
    assert result.complete_cost_per_resolution == Decimal(5)
    assert result.billable_activation_allowed is False


def shared_cost_observation(inputs: dict[str, Any]) -> dict[str, object]:
    from accounting_support import context
    from case_promise_support import communication, invoices
    from identity_support import actor_client

    from aco.core.types import CostQuality, ProviderCost
    from aco.features.cases.dto import CommunicationEvidence, UpsertCase
    from aco.features.cases.service import CaseService
    from aco.features.identity.models import ProviderConnection, now
    from aco.features.usage.dto import ProviderUsageInput
    from aco.features.usage.models import UsageSource
    from aco.features.usage.service import UsageService

    with actor_client("owner") as (client, db, user, tenant):
        invoice_aliases = sorted({leaf["invoice"] for leaf in inputs["leaves"]})
        selected = invoices(client, db, tenant)[: len(invoice_aliases)]
        aliases = dict(zip(invoice_aliases, selected, strict=True))
        body = "SYNTHETIC normative shared operation source; no live call or customer quote."
        message = communication(db, tenant, selected, body)
        ctx = context(tenant, user.id)
        for case_alias in sorted({leaf["case"] for leaf in inputs["leaves"]}):
            linked = [aliases[leaf["invoice"]] for leaf in inputs["leaves"] if leaf["case"] == case_alias]
            CaseService(db).upsert_case(
                ctx,
                UpsertCase(
                    customer_id=linked[0].customer_id,
                    invoice_ids=[row.id for row in linked],
                    issue_type="wrong_amount",
                    claim_key=case_alias,
                    substance="Synthetic shared-source issue",
                    evidence=CommunicationEvidence(communication_id=message.id, excerpt=body, offset=0),
                ),
            )
        connection = db.get(ProviderConnection, message.provider_connection_id)
        assert connection is not None
        command = ProviderUsageInput(
            provider_connection_id=connection.id,
            operation_key=inputs["operation"],
            component=inputs["component"],
            operation="explicit_synthetic_fixture",
            provider_kind=connection.provider_type,
            provider=connection.provider,
            synthetic=True,
            cost=ProviderCost(
                amount=Decimal(inputs["charge"]),
                currency=inputs["currency"],
                quality=CostQuality.PROVIDER_REPORTED,
            ),
        )
        event = UsageService(db).record_usage(ctx, command)
        assert UsageService(db).record_usage(ctx, command).id == event.id
        db.add(
            UsageSource(
                event_id=event.id,
                source_kind="communication",
                source_id=message.id,
                component=event.component,
            )
        )
        db.commit()
        base = f"/api/workspaces/{tenant}/impact/economics"
        params = {
            "since": (event.created_at - timedelta(seconds=1)).isoformat(),
            "until": (now() + timedelta(days=1)).isoformat(),
        }
        read = client.get(base, params=params)
        assert read.status_code == 200, read.text
        original = next(row for row in read.json()["events"] if row["event_id"] == str(event.id))
        allocation = client.post(
            base + f"/usage/{event.id}/allocation",
            json={
                "request_id": str(uuid4()),
                "expected_revision_id": original["revision_id"],
                "reviewed_source_hash": original["allocation"]["source_hash"],
                "expected_version_id": None,
                "leaves": None,
                "evidence": "Owner reviewed exact synthetic three-leaf source attribution.",
            },
        )
        assert allocation.status_code == 201, allocation.text
        saved = client.get(base, params=params)
        assert saved.status_code == 200, saved.text
        events = [row for row in saved.json()["events"] if row["event_id"] == str(event.id)]
        weights = [Decimal(leaf["weight"]) for leaf in events[0]["allocation"]["leaves"]]
        return {
            "unique_charge_count": len(events),
            "total_cost": format(
                sum((Decimal(row["amount"]) for row in events), Decimal(0)).normalize(), "f"
            ),
            "weights": [format(weight, "f") for weight in weights],
            "weight_sum": format(sum(weights), "f"),
        }
