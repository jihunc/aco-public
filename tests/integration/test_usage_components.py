"""Exact wire totals, currencies, rates and explicitly contained vendor bundles."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi import HTTPException
from identity_support import actor_client
from sqlalchemy import select
from usage_support import setup

from aco.core.types import CostQuality, ProviderCost
from aco.features.identity.models import now
from aco.features.usage.dto import ProviderUsageInput, Units
from aco.features.usage.models import ProviderUsageEvent, RateSnapshot, UsageRevision
from aco.features.usage.service import UsageService


def value(connection: UUID, **updates: Any) -> ProviderUsageInput:
    return ProviderUsageInput(
        provider_connection_id=connection,
        operation_key="exact-bundle",
        component="provider_total",
        operation="voice",
        provider_kind="llm",
        provider="mock",
        synthetic=True,
        cost=ProviderCost(
            amount=Decimal("0.000000000003"), currency="USD", quality=CostQuality.PROVIDER_REPORTED
        ),
        units=Units(
            input_tokens=3, output_tokens=2, cached_tokens=1, messages=1, duration_seconds=Decimal("0.125")
        ),
        **updates,
    )


def test_component_fixture_exact_sum_and_mixed_currencies_on_real_wire() -> None:
    from decimal import localcontext

    fixture = json.loads(Path("tests/fixtures/usage/provider_components.json").read_text())
    with actor_client("owner") as (client, store, user, tenant):
        ctx, connection, authority, scopes = setup(store, tenant, user)
        for case in fixture["cases"]:
            item = value(connection).model_copy(
                update={
                    "component": case["component"],
                    "bundle": case.get("bundle"),
                    "included_in": case.get("included_in"),
                    "cost": ProviderCost(
                        amount=Decimal(case["amount"]) if case["amount"] is not None else None,
                        currency="USD",
                        quality=CostQuality(case["quality"]),
                    ),
                }
            )
            UsageService(store).record_usage(ctx, item)
        UsageService(store).record_usage(
            ctx,
            value(connection).model_copy(
                update={
                    "operation_key": "aud",
                    "cost": ProviderCost(
                        amount=Decimal("2"), currency="AUD", quality=CostQuality.PROVIDER_REPORTED
                    ),
                }
            ),
        )
        store.commit()
        response = client.get(f"/api/workspaces/{tenant}/usage")
        assert response is not None
        assert response.status_code == 200
        rows = {row["currency"]: row for row in response.json()["currencies"]}
        with localcontext() as precision:
            precision.prec = 80
            assert Decimal(rows["USD"]["known_amount"]) == Decimal("99999999999999999999999999.123456789015")
        assert rows["USD"]["unknown_count"] == 1 and rows["USD"]["completeness"] == "partial"
        assert rows["USD"]["synthetic"] is True
        assert rows["AUD"]["known_amount"] == "2.000000000000"
        assert all(isinstance(row["known_amount"], str) for row in rows.values())


def test_contained_component_requires_same_operation_bundle_currency_and_parent() -> None:
    with actor_client("owner") as (client, store, user, tenant):
        ctx, connection, authority, scopes = setup(store, tenant, user)
        item = value(connection).model_copy(
            update={"component": "carrier", "included_in": "provider_total", "bundle": "voice"}
        )
        with pytest.raises(HTTPException, match="BUNDLE_PARENT_UNVERIFIED"):
            UsageService(store).record_usage(ctx, item)
        parent = value(connection, bundle="voice")
        UsageService(store).record_usage(ctx, parent)
        bad = item.model_copy(
            update={
                "cost": ProviderCost(
                    amount=Decimal("1"), currency="AUD", quality=CostQuality.PROVIDER_REPORTED
                )
            }
        )
        with pytest.raises(HTTPException, match="BUNDLE_PARENT_UNVERIFIED"):
            UsageService(store).record_usage(ctx, bad)


def test_rate_snapshot_identity_and_original_estimate_preserved_on_reconciliation() -> None:
    with actor_client("owner") as (client, store, user, tenant):
        ctx, connection, authority, scopes = setup(store, tenant, user)
        rate = RateSnapshot(
            tenant_id=tenant,
            provider="mock",
            currency="USD",
            unit="input_tokens",
            amount=Decimal("0.000000000001"),
            price_source_at=now(),
            synthetic=True,
        )
        store.add(rate)
        store.flush()
        estimate = value(connection).model_copy(
            update={
                "reconciliation_key": "estimated",
                "units": Units(input_tokens=4, cached_tokens=1),
                "price_source_at": rate.price_source_at,
                "cost": ProviderCost(
                    amount=Decimal("0.000000000003"),
                    currency="USD",
                    quality=CostQuality.RATE_ESTIMATE,
                    rate_snapshot_id=rate.id,
                ),
            }
        )
        row = UsageService(store).record_usage(ctx, estimate)
        actual = estimate.model_copy(
            update={
                "reconciliation_key": "reported",
                "cost": ProviderCost(
                    amount=Decimal("0.000000000005"), currency="USD", quality=CostQuality.PROVIDER_REPORTED
                ),
            }
        )
        UsageService(store).record_usage(ctx, actual)
        store.commit()
        revisions = list(
            store.scalars(
                select(UsageRevision).where(UsageRevision.event_id == row.id).order_by(UsageRevision.version)
            )
        )
        assert [r.cost_amount for r in revisions] == [Decimal("0.000000000003"), Decimal("0.000000000005")]
        bad_rate = RateSnapshot(
            tenant_id=tenant,
            provider="different",
            currency="USD",
            unit="input_tokens",
            amount=Decimal("0.000000000009"),
            price_source_at=now(),
            synthetic=True,
        )
        store.add(bad_rate)
        store.flush()
        with pytest.raises(HTTPException, match="RATE_SCOPE_CONFLICT"):
            UsageService(store).record_usage(
                ctx,
                estimate.model_copy(
                    update={
                        "operation_key": "bad-rate",
                        "cost": estimate.cost.model_copy(update={"rate_snapshot_id": bad_rate.id}),
                    }
                ),
            )


def test_cached_input_estimate_uses_nonoverlapping_rate_basis() -> None:
    with actor_client("owner") as (client, store, user, tenant):
        ctx, connection, authority, scopes = setup(store, tenant, user)
        rate = RateSnapshot(
            tenant_id=tenant,
            provider="mock",
            currency="USD",
            unit="input_tokens",
            amount=Decimal("0.000000000001"),
            price_source_at=now(),
            synthetic=True,
        )
        store.add(rate)
        store.flush()
        # Native input includes 2 cached of 5 input; charge only 3 uncached here.
        item = value(connection).model_copy(
            update={
                "component": "uncached_input",
                "units": Units(input_tokens=5, cached_tokens=2),
                "price_source_at": rate.price_source_at,
                "cost": ProviderCost(
                    amount=Decimal("0.000000000005"),
                    currency="USD",
                    quality=CostQuality.RATE_ESTIMATE,
                    rate_snapshot_id=rate.id,
                ),
            }
        )
        with pytest.raises(HTTPException, match="RATE_ESTIMATE_MISMATCH"):
            UsageService(store).record_usage(ctx, item)
        valid = item.model_copy(
            update={"cost": item.cost.model_copy(update={"amount": Decimal("0.000000000003")})}
        )
        UsageService(store).record_usage(ctx, valid)


def test_new_estimate_requires_snapshot_and_cache_cannot_exceed_input() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Units(input_tokens=1, cached_tokens=2)
    with actor_client("owner") as (client, store, user, tenant):
        ctx, connection, authority, scopes = setup(store, tenant, user)
        item = value(connection).model_copy(
            update={
                "cost": ProviderCost(amount=Decimal("0.1"), currency="USD", quality=CostQuality.RATE_ESTIMATE)
            }
        )
        with pytest.raises(HTTPException, match="RATE_SNAPSHOT_REQUIRED"):
            UsageService(store).record_usage(ctx, item)


def test_reported_estimated_unknown_bases_stay_distinct_in_database_and_api() -> None:
    with actor_client("owner") as (client, store, user, tenant):
        ctx, connection, authority, scopes = setup(store, tenant, user)
        rate = RateSnapshot(
            tenant_id=tenant,
            provider="mock",
            currency="USD",
            unit="requests",
            amount=Decimal("0.000000000002"),
            price_source_at=now(),
            synthetic=True,
        )
        store.add(rate)
        store.flush()
        service = UsageService(store)
        service.record_usage(ctx, value(connection))
        service.record_usage(
            ctx,
            value(connection).model_copy(
                update={
                    "operation_key": "estimated",
                    "price_source_at": rate.price_source_at,
                    "cost": ProviderCost(
                        amount=rate.amount,
                        currency="USD",
                        quality=CostQuality.RATE_ESTIMATE,
                        rate_snapshot_id=rate.id,
                    ),
                }
            ),
        )
        service.record_usage(
            ctx,
            value(connection).model_copy(
                update={
                    "operation_key": "unknown",
                    "cost": ProviderCost(amount=None, currency="USD", quality=CostQuality.UNKNOWN),
                }
            ),
        )
        store.commit()
        rows = client.get(f"/api/workspaces/{tenant}/usage").json()["currencies"]
        row = next(row for row in rows if row["currency"] == "USD" and row["synthetic"])
        assert row["reported_amount"] == "0.000000000003"
        assert row["estimated_amount"] == "0.000000000002"
        assert row["known_amount"] == "0.000000000005"
        assert row["unknown_count"] == 1 and row["completeness"] == "partial"
        assert row["reserved_amount"] == "0" and row["spent_amount"] == "0"
        assert sorted(
            store.scalars(
                select(UsageRevision.cost_quality)
                .join(ProviderUsageEvent)
                .where(ProviderUsageEvent.tenant_id == tenant)
            )
        ) == ["PROVIDER_REPORTED", "RATE_ESTIMATE", "UNKNOWN"]


@pytest.mark.parametrize(
    "amount,currency,quality",
    [
        (None, None, "UNKNOWN"),
        (Decimal("NaN"), "USD", "PROVIDER_REPORTED"),
        (None, "USD", "PROVIDER_REPORTED"),
    ],
)
def test_database_rejects_unpriced_tenant_currency_and_nonfinite_charge(
    amount: Decimal | None, currency: str | None, quality: str
) -> None:
    from sqlalchemy.exc import DBAPIError

    with actor_client("owner") as (client, store, user, tenant):
        ctx, connection, authority, scopes = setup(store, tenant, user)
        event = UsageService(store).record_usage(ctx, value(connection))
        with pytest.raises(DBAPIError):
            with store.begin_nested():
                store.add(
                    UsageRevision(
                        event_id=event.id,
                        reconciliation_key="invalid",
                        cost_amount=amount,
                        currency=currency,
                        cost_quality=quality,
                        outcome="UNKNOWN",
                        payload={},
                    )
                )
                store.flush()


def test_usage_connection_and_time_filters_preserve_provider_basis_and_reject_foreign_scope() -> None:
    from datetime import timedelta
    from uuid import uuid4

    with actor_client("owner") as (client, store, user, tenant):
        ctx, one, authority, scopes = setup(store, tenant, user)
        ctx, two, authority, scopes = setup(store, tenant, user)
        cutoff = now()
        for connection in (one, two):
            UsageService(store).record_usage(ctx, value(connection))
        store.commit()
        path = f"/api/workspaces/{tenant}/usage"
        response = client.get(path, params={"provider_connection_id": str(one), "since": cutoff.isoformat()})
        assert response.status_code == 200
        view = response.json()
        assert view["currencies"][0]["reported_amount"] == "0.000000000003"
        assert view["providers"][0]["provider_connection_id"] == str(one)
        assert view["providers"][0]["provider"] == "mock"
        assert view["providers"][0]["reported_amount"] == "0.000000000003"
        assert client.get(path, params={"until": cutoff.isoformat()}).json()["currencies"] == []
        assert (
            client.get(
                path, params={"since": cutoff.isoformat(), "until": (cutoff - timedelta(days=1)).isoformat()}
            ).status_code
            == 422
        )
        assert client.get(path, params={"since": "2026-01-01"}).status_code == 422
        assert client.get(path, params={"provider_connection_id": str(uuid4())}).status_code == 403
        assert client.get(path, params={"provider_connection_id": "invalid"}).status_code == 422
        assert client.get(path, params={"unsupported": "true"}).status_code == 422


def test_unknown_contained_component_keeps_price_coverage_partial() -> None:
    with actor_client("owner") as (client, store, user, tenant):
        ctx, connection, authority, scopes = setup(store, tenant, user)
        parent = value(connection).model_copy(
            update={
                "bundle": "voice",
                "cost": ProviderCost(
                    amount=Decimal("0.10"), currency="USD", quality=CostQuality.PROVIDER_REPORTED
                ),
            }
        )
        service = UsageService(store)
        service.record_usage(ctx, parent)
        service.record_usage(
            ctx,
            parent.model_copy(
                update={
                    "component": "carrier",
                    "included_in": "provider_total",
                    "cost": ProviderCost(amount=None, currency="USD", quality=CostQuality.UNKNOWN),
                }
            ),
        )
        store.commit()
        response = client.get(f"/api/workspaces/{tenant}/usage")
        assert response.status_code == 200
        summary = response.json()
        currency, provider = summary["currencies"][0], summary["providers"][0]
        assert Decimal(currency["known_amount"]) == Decimal("0.10")
        assert currency["unknown_count"] == provider["unknown_count"] == 1
        assert currency["completeness"] == "partial"
        print("F3 persisted", tenant, currency, provider)


@pytest.mark.parametrize("correction", ["smaller", "unknown", "bundle"])
def test_parent_corrections_preserve_child_facts_and_expose_partial_bundle(correction: str) -> None:
    with actor_client("owner") as (client, store, user, tenant):
        ctx, connection, authority, scopes = setup(store, tenant, user)
        service = UsageService(store)
        parent_input = value(connection).model_copy(
            update={
                "bundle": "voice",
                "cost": ProviderCost(
                    amount=Decimal("0.10"), currency="USD", quality=CostQuality.PROVIDER_REPORTED
                ),
            }
        )
        parent = service.record_usage(ctx, parent_input)
        child = service.record_usage(
            ctx,
            parent_input.model_copy(
                update={
                    "component": "carrier",
                    "included_in": "provider_total",
                    "cost": ProviderCost(
                        amount=Decimal("0.08"), currency="USD", quality=CostQuality.PROVIDER_REPORTED
                    ),
                }
            ),
        )
        store.commit()
        original = [
            (r.id, r.cost_amount, r.payload)
            for r in store.scalars(
                select(UsageRevision)
                .join(ProviderUsageEvent)
                .where(ProviderUsageEvent.tenant_id == tenant)
                .order_by(UsageRevision.id)
            )
        ]
        changed = parent_input.model_copy(update={"reconciliation_key": "corrected"})
        if correction == "bundle":
            changed = changed.model_copy(update={"bundle": "different"})
        else:
            changed = changed.model_copy(
                update={
                    "cost": ProviderCost(
                        amount=None if correction == "unknown" else Decimal("0.02"),
                        currency="USD",
                        quality=CostQuality.UNKNOWN
                        if correction == "unknown"
                        else CostQuality.PROVIDER_REPORTED,
                    )
                }
            )
        service.record_usage(ctx, changed)
        store.commit()
        response = client.get(f"/api/workspaces/{tenant}/usage")
        assert response.status_code == 200
        summary = response.json()
        currency, provider = summary["currencies"][0], summary["providers"][0]
        assert currency["completeness"] == provider["completeness"] == "partial"
        assert currency["bundle_issue_count"] == provider["bundle_issue_count"] == 1
        assert currency["unknown_count"] == (1 if correction == "unknown" else 0)
        issue = summary["bundle_issues"][0]
        assert issue["parent_event_id"] == str(parent.id) and issue["event_id"] == str(child.id)
        assert issue["component_amount"] == "0.080000000000" and issue["currency"] == "USD"
        assert issue["parent_amount"] == (
            None
            if correction == "unknown"
            else "0.100000000000"
            if correction == "bundle"
            else "0.020000000000"
        )
        assert issue["reason"] in {
            "PARENT_AMOUNT_UNKNOWN",
            "COMPONENT_EXCEEDS_PARENT",
            "BUNDLE_IDENTITY_CONFLICT",
        }
        expected = (
            Decimal(0)
            if correction == "unknown"
            else Decimal("0.10")
            if correction == "bundle"
            else Decimal("0.02")
        )
        assert Decimal(currency["known_amount"]) == expected
        current = {
            r.id: (r.cost_amount, r.payload)
            for r in store.scalars(
                select(UsageRevision).join(ProviderUsageEvent).where(ProviderUsageEvent.tenant_id == tenant)
            )
        }
        assert len(current) == 3 and all(
            current[key] == (amount, payload) for key, amount, payload in original
        )
        assert any("Unresolved bundle" in notice for notice in summary["notices"])
        print("F4 persisted", tenant, correction, currency, issue)


@pytest.mark.parametrize("transition", ["self_cycle", "reparent", "currency", "filter"])
def test_latest_containment_topology_and_scope_never_hide_known_facts(transition: str) -> None:
    from aco.features.usage.dto import UsageFilters

    with actor_client("owner") as (client, store, user, tenant):
        ctx, connection, authority, scopes = setup(store, tenant, user)
        service = UsageService(store)
        base = value(connection).model_copy(
            update={
                "bundle": "voice",
                "cost": ProviderCost(
                    amount=Decimal("0.10"), currency="USD", quality=CostQuality.PROVIDER_REPORTED
                ),
            }
        )
        parent = service.record_usage(ctx, base)
        cutoff = now()
        child = service.record_usage(
            ctx,
            base.model_copy(
                update={
                    "component": "carrier",
                    "included_in": "provider_total",
                    "cost": ProviderCost(
                        amount=Decimal("0.08"), currency="USD", quality=CostQuality.PROVIDER_REPORTED
                    ),
                }
            ),
        )
        if transition == "currency":
            with pytest.raises(HTTPException, match="USAGE_CURRENCY_CONFLICT"):
                service.record_usage(
                    ctx,
                    base.model_copy(
                        update={
                            "reconciliation_key": "currency",
                            "cost": ProviderCost(
                                amount=Decimal("0.10"), currency="AUD", quality=CostQuality.PROVIDER_REPORTED
                            ),
                        }
                    ),
                )
        elif transition != "filter":
            included_in = "provider_total"
            if transition == "reparent":
                included_in = "grand_total"
                service.record_usage(
                    ctx,
                    base.model_copy(
                        update={
                            "component": included_in,
                            "cost": ProviderCost(
                                amount=Decimal("0.20"), currency="USD", quality=CostQuality.PROVIDER_REPORTED
                            ),
                        }
                    ),
                )
            service.record_usage(
                ctx, base.model_copy(update={"reconciliation_key": "topology", "included_in": included_in})
            )
        store.commit()
        summary = service.list_usage(ctx, UsageFilters(since=cutoff) if transition == "filter" else None)
        currency = summary.currencies[0]
        if transition == "currency":
            assert currency.completeness == "complete" and currency.known_amount == "0.100000000000"
            assert (
                store.scalar(
                    select(UsageRevision).where(
                        UsageRevision.event_id == parent.id, UsageRevision.version == 2
                    )
                )
                is None
            )
        else:
            assert currency.completeness == "partial" and summary.providers[0].completeness == "partial"
            expected = "PARENT_OUTSIDE_FILTER" if transition == "filter" else "BUNDLE_IDENTITY_CONFLICT"
            assert any(
                issue.event_id == child.id and issue.reason == expected for issue in summary.bundle_issues
            )
            assert Decimal(currency.known_amount) == (
                Decimal("0.20") if transition == "reparent" else Decimal(0)
            )
        assert store.scalar(
            select(UsageRevision.cost_amount).where(UsageRevision.event_id == child.id)
        ) == Decimal("0.08")
        print("F4 topology", tenant, transition, summary.model_dump(mode="json"))


@pytest.mark.parametrize(
    "parent_amount,corrected,child_amount,coverage,issues",
    [
        ("0.20", "0.10", "0.08", "partial", 2),
        ("0.20", "0.16", "0.08", "complete", 0),
        ("0.20", "0.17", "0.08", "complete", 0),
        (
            "90000000000000000000000000",
            "80000000000000000000000000.000000000001",
            "40000000000000000000000000.000000000001",
            "partial",
            2,
        ),
        (
            "90000000000000000000000000",
            "80000000000000000000000000.000000000002",
            "40000000000000000000000000.000000000001",
            "complete",
            0,
        ),
    ],
)
def test_distinct_latest_siblings_cannot_claim_complete_below_their_known_amounts(
    parent_amount: str, corrected: str, child_amount: str, coverage: str, issues: int
) -> None:
    from sqlalchemy.orm import Session

    with actor_client("owner") as (client, store, user, tenant):
        ctx, connection, authority, scopes = setup(store, tenant, user)
        service = UsageService(store)
        base = value(connection).model_copy(
            update={
                "bundle": "voice",
                "cost": ProviderCost(
                    amount=Decimal(parent_amount), currency="USD", quality=CostQuality.PROVIDER_REPORTED
                ),
            }
        )
        parent = service.record_usage(ctx, base)
        children = [
            service.record_usage(
                ctx,
                base.model_copy(
                    update={
                        "component": component,
                        "included_in": "provider_total",
                        "cost": ProviderCost(
                            amount=Decimal(child_amount),
                            currency="USD",
                            quality=CostQuality.PROVIDER_REPORTED,
                        ),
                    }
                ),
            )
            for component in ("carrier", "tts")
        ]
        service.record_usage(
            ctx,
            base.model_copy(
                update={
                    "reconciliation_key": "corrected",
                    "cost": ProviderCost(
                        amount=Decimal(corrected), currency="USD", quality=CostQuality.PROVIDER_REPORTED
                    ),
                }
            ),
        )
        store.commit()
        with Session(store.get_bind()) as independent:
            rows = list(
                independent.scalars(
                    select(UsageRevision).where(
                        UsageRevision.event_id.in_([parent.id, *(child.id for child in children)])
                    )
                )
            )
            assert len(rows) == 4
            assert sorted((r.payload["component"], r.version, r.cost_amount) for r in rows) == [
                ("carrier", 1, Decimal(child_amount)),
                ("provider_total", 1, Decimal(parent_amount)),
                ("provider_total", 2, Decimal(corrected)),
                ("tts", 1, Decimal(child_amount)),
            ]
            assert all(r.cost_quality == "PROVIDER_REPORTED" and r.currency == "USD" for r in rows)
        response = client.get(f"/api/workspaces/{tenant}/usage")
        assert response.status_code == 200
        summary = response.json()
        currency, provider = summary["currencies"][0], summary["providers"][0]
        assert currency["completeness"] == provider["completeness"] == coverage
        assert currency["bundle_issue_count"] == provider["bundle_issue_count"] == issues
        assert currency["unknown_count"] == provider["unknown_count"] == 0
        assert Decimal(currency["known_amount"]) == Decimal(corrected)
        assert {issue["event_id"] for issue in summary["bundle_issues"]} == (
            {str(child.id) for child in children} if issues else set()
        )
        for issue in summary["bundle_issues"]:
            assert issue["reason"] == "BUNDLE_COMPONENTS_EXCEED_PARENT"
            assert issue["parent_event_id"] == str(parent.id)
            assert Decimal(issue["parent_amount"]) == Decimal(corrected)
            assert Decimal(issue["component_amount"]) == Decimal(child_amount)
        print(
            "repair2 sibling",
            tenant,
            parent.id,
            [child.id for child in children],
            currency,
            summary["bundle_issues"],
        )


def test_child_corrections_introduce_and_resolve_sibling_conflict_without_summing_history() -> None:
    from sqlalchemy.orm import Session

    with actor_client("owner") as (client, store, user, tenant):
        ctx, connection, authority, scopes = setup(store, tenant, user)
        service = UsageService(store)
        base = value(connection).model_copy(
            update={
                "bundle": "voice",
                "cost": ProviderCost(
                    amount=Decimal("0.10"), currency="USD", quality=CostQuality.PROVIDER_REPORTED
                ),
            }
        )
        parent = service.record_usage(ctx, base)
        for component in ("carrier", "tts"):
            service.record_usage(
                ctx,
                base.model_copy(
                    update={
                        "component": component,
                        "included_in": "provider_total",
                        "cost": ProviderCost(
                            amount=Decimal("0.04"), currency="USD", quality=CostQuality.PROVIDER_REPORTED
                        ),
                    }
                ),
            )
        store.commit()
        original = service.list_usage(ctx)
        assert original.currencies[0].completeness == "complete"
        for key, amount, coverage in (("increase", "0.08", "partial"), ("resolve", "0.06", "complete")):
            update = base.model_copy(
                update={
                    "component": "carrier",
                    "included_in": "provider_total",
                    "reconciliation_key": key,
                    "cost": ProviderCost(
                        amount=Decimal(amount), currency="USD", quality=CostQuality.PROVIDER_REPORTED
                    ),
                }
            )
            child = service.record_usage(ctx, update)
            service.record_usage(ctx, update)
            store.commit()
            summary = client.get(f"/api/workspaces/{tenant}/usage").json()
            assert summary["currencies"][0]["completeness"] == coverage
            assert Decimal(summary["currencies"][0]["known_amount"]) == Decimal("0.10")
            assert summary["currencies"][0]["unknown_count"] == 0
        with Session(store.get_bind()) as independent:
            history = list(
                independent.scalars(
                    select(UsageRevision)
                    .where(UsageRevision.event_id == child.id)
                    .order_by(UsageRevision.version)
                )
            )
            assert [r.cost_amount for r in history] == [Decimal("0.04"), Decimal("0.08"), Decimal("0.06")]
            assert independent.scalar(
                select(UsageRevision.cost_amount).where(UsageRevision.event_id == parent.id)
            ) == Decimal("0.10")
        assert summary["bundle_issues"] == []


@pytest.mark.parametrize("filter_mode", ["none", "parent_only", "one_child"])
def test_sibling_context_outside_time_filter_keeps_relevant_bundle_partial(filter_mode: str) -> None:
    with actor_client("owner") as (client, store, user, tenant):
        ctx, connection, authority, scopes = setup(store, tenant, user)
        service = UsageService(store)
        base = value(connection).model_copy(
            update={
                "bundle": "voice",
                "cost": ProviderCost(
                    amount=Decimal("0.20"), currency="USD", quality=CostQuality.PROVIDER_REPORTED
                ),
            }
        )
        service.record_usage(ctx, base)
        after_parent = now()
        carrier = service.record_usage(
            ctx,
            base.model_copy(
                update={
                    "component": "carrier",
                    "included_in": "provider_total",
                    "cost": ProviderCost(
                        amount=Decimal("0.08"), currency="USD", quality=CostQuality.PROVIDER_REPORTED
                    ),
                }
            ),
        )
        after_carrier = now()
        tts = service.record_usage(
            ctx,
            base.model_copy(
                update={
                    "component": "tts",
                    "included_in": "provider_total",
                    "cost": ProviderCost(
                        amount=Decimal("0.08"), currency="USD", quality=CostQuality.PROVIDER_REPORTED
                    ),
                }
            ),
        )
        service.record_usage(
            ctx,
            base.model_copy(
                update={
                    "reconciliation_key": "corrected",
                    "cost": ProviderCost(
                        amount=Decimal("0.10"), currency="USD", quality=CostQuality.PROVIDER_REPORTED
                    ),
                }
            ),
        )
        store.commit()
        params = (
            {"until": after_parent.isoformat()}
            if filter_mode == "parent_only"
            else {"since": after_parent.isoformat(), "until": after_carrier.isoformat()}
            if filter_mode == "one_child"
            else {}
        )
        response = client.get(f"/api/workspaces/{tenant}/usage", params=params)
        assert response.status_code == 200
        summary = response.json()
        assert (
            summary["currencies"][0]["completeness"] == summary["providers"][0]["completeness"] == "partial"
        )
        assert {issue["event_id"] for issue in summary["bundle_issues"]} == {str(carrier.id), str(tts.id)}
        assert {issue["reason"] for issue in summary["bundle_issues"]} == {"BUNDLE_COMPONENTS_EXCEED_PARENT"}
        assert {issue["component"] for issue in summary["bundle_issues"] if issue["outside_filter"]} == (
            {"carrier", "tts"}
            if filter_mode == "parent_only"
            else {"tts"}
            if filter_mode == "one_child"
            else set()
        )
        assert Decimal(summary["currencies"][0]["known_amount"]) == (
            Decimal(0) if filter_mode == "one_child" else Decimal("0.10")
        )
        assert summary["currencies"][0]["unknown_count"] == 0


def test_sibling_consistency_isolated_by_current_parent_connection_operation_and_basis() -> None:
    with actor_client("owner") as (client, store, user, tenant):
        ctx, first, authority, scopes = setup(store, tenant, user)
        ctx, second, authority, scopes = setup(store, tenant, user)
        service = UsageService(store)
        for connection, operation, parent_component, child_component, currency, synthetic in (
            (first, "one", "provider_total", "carrier", "USD", True),
            (second, "one", "provider_total", "carrier", "USD", True),
            (first, "two", "provider_total", "carrier", "USD", True),
            (first, "one", "alternate_total", "tts", "USD", True),
            (first, "aud", "provider_total", "carrier", "AUD", True),
            (first, "observed", "provider_total", "carrier", "USD", False),
        ):
            base = value(connection).model_copy(
                update={
                    "operation_key": operation,
                    "component": parent_component,
                    "bundle": "same-label",
                    "synthetic": synthetic,
                    "cost": ProviderCost(
                        amount=Decimal("0.10"), currency=currency, quality=CostQuality.PROVIDER_REPORTED
                    ),
                }
            )
            service.record_usage(ctx, base)
            service.record_usage(
                ctx,
                base.model_copy(
                    update={
                        "component": child_component,
                        "included_in": parent_component,
                        "cost": ProviderCost(
                            amount=Decimal("0.08"), currency=currency, quality=CostQuality.PROVIDER_REPORTED
                        ),
                    }
                ),
            )
        store.commit()
        summary = client.get(f"/api/workspaces/{tenant}/usage").json()
        assert summary["bundle_issues"] == []
        assert {
            (row["currency"], row["synthetic"]): Decimal(row["known_amount"]) for row in summary["currencies"]
        } == {("USD", True): Decimal("0.40"), ("AUD", True): Decimal("0.10"), ("USD", False): Decimal("0.10")}
        assert all(row["completeness"] == "complete" for row in summary["currencies"] + summary["providers"])
        filtered = client.get(
            f"/api/workspaces/{tenant}/usage", params={"provider_connection_id": str(second)}
        ).json()
        assert (
            len(filtered["currencies"]) == 1 and filtered["currencies"][0]["known_amount"] == "0.100000000000"
        )
        assert filtered["bundle_issues"] == []


def test_invalidated_bundle_sibling_is_flagged_without_joining_a_different_current_bundle() -> None:
    with actor_client("owner") as (client, store, user, tenant):
        ctx, connection, authority, scopes = setup(store, tenant, user)
        service = UsageService(store)
        base = value(connection).model_copy(
            update={
                "bundle": "old",
                "cost": ProviderCost(
                    amount=Decimal("0.10"), currency="USD", quality=CostQuality.PROVIDER_REPORTED
                ),
            }
        )
        service.record_usage(ctx, base)
        old_child = service.record_usage(
            ctx,
            base.model_copy(
                update={
                    "component": "carrier",
                    "included_in": "provider_total",
                    "cost": ProviderCost(
                        amount=Decimal("0.08"), currency="USD", quality=CostQuality.PROVIDER_REPORTED
                    ),
                }
            ),
        )
        current = base.model_copy(update={"bundle": "current", "reconciliation_key": "new-bundle"})
        service.record_usage(ctx, current)
        service.record_usage(
            ctx,
            current.model_copy(
                update={
                    "component": "tts",
                    "included_in": "provider_total",
                    "cost": ProviderCost(
                        amount=Decimal("0.08"), currency="USD", quality=CostQuality.PROVIDER_REPORTED
                    ),
                }
            ),
        )
        store.commit()
        summary = client.get(f"/api/workspaces/{tenant}/usage").json()
        assert [(issue["event_id"], issue["reason"]) for issue in summary["bundle_issues"]] == [
            (str(old_child.id), "BUNDLE_IDENTITY_CONFLICT")
        ]
        assert summary["currencies"][0]["known_amount"] == "0.100000000000"
        assert summary["currencies"][0]["unknown_count"] == 0


def test_parent_reconciliation_resolves_sibling_conflict_without_rewriting_original_facts() -> None:
    from sqlalchemy.orm import Session

    with actor_client("owner") as (client, store, user, tenant):
        ctx, connection, authority, scopes = setup(store, tenant, user)
        service = UsageService(store)
        base = value(connection).model_copy(
            update={
                "bundle": "voice",
                "cost": ProviderCost(
                    amount=Decimal("0.20"), currency="USD", quality=CostQuality.PROVIDER_REPORTED
                ),
            }
        )
        parent = service.record_usage(ctx, base)
        for component in ("carrier", "tts"):
            service.record_usage(
                ctx,
                base.model_copy(
                    update={
                        "component": component,
                        "included_in": "provider_total",
                        "cost": ProviderCost(
                            amount=Decimal("0.08"), currency="USD", quality=CostQuality.PROVIDER_REPORTED
                        ),
                    }
                ),
            )
        for key, amount, coverage in (
            ("inconsistent", "0.10", "partial"),
            ("reconciled", "0.16", "complete"),
        ):
            service.record_usage(
                ctx,
                base.model_copy(
                    update={
                        "reconciliation_key": key,
                        "cost": ProviderCost(
                            amount=Decimal(amount), currency="USD", quality=CostQuality.PROVIDER_REPORTED
                        ),
                    }
                ),
            )
            store.commit()
            summary = client.get(f"/api/workspaces/{tenant}/usage").json()
            assert (
                summary["currencies"][0]["completeness"]
                == summary["providers"][0]["completeness"]
                == coverage
            )
            assert Decimal(summary["currencies"][0]["known_amount"]) == Decimal(amount)
        assert summary["bundle_issues"] == []
        with Session(store.get_bind()) as independent:
            rows = list(
                independent.scalars(
                    select(UsageRevision)
                    .join(ProviderUsageEvent)
                    .where(ProviderUsageEvent.tenant_id == tenant)
                )
            )
            assert len(rows) == 5
            assert sorted(
                r.cost_amount for r in rows if r.event_id != parent.id and r.cost_amount is not None
            ) == [
                Decimal("0.08"),
                Decimal("0.08"),
            ]
            assert [
                (r.version, r.cost_amount)
                for r in sorted(rows, key=lambda row: row.version)
                if r.event_id == parent.id
            ] == [(1, Decimal("0.20")), (2, Decimal("0.10")), (3, Decimal("0.16"))]
