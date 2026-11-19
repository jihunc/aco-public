"""Trusted synthetic test setup uses the production ledger and budget services."""

import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from typing import Any, cast
from uuid import UUID, uuid4

from boundary_evidence import BoundaryObserver
from fastapi import FastAPI
from identity_support import actor_client
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.core.types import CostQuality, ProviderCost, Role
from aco.features.identity.models import ProviderConnection, User
from aco.features.usage.budgets import BudgetService, SyntheticAuthority
from aco.features.usage.dto import ProviderUsageInput, UsageOutcome, UsageReservationRequest
from aco.features.usage.models import BudgetReservation, ProviderUsageEvent
from aco.features.usage.service import UsageService


def setup(
    store: Session, tenant: UUID, user: User, cap: Decimal = Decimal("0.10"), request_cap: int = 100
) -> tuple[TenantContext, UUID, SyntheticAuthority, tuple[UUID, ...]]:
    connection = ProviderConnection(tenant_id=tenant, provider="mock", provider_type="llm", status="READY")
    store.add(connection)
    store.commit()
    ctx = TenantContext(tenant_id=tenant, actor_id=user.id, role=Role.OWNER, correlation_id=uuid4())
    service = BudgetService(store)
    authority = service.synthetic_authority(ctx, connection.id, "local")
    scopes = tuple(
        service.create_scope(ctx, authority, dimension).id
        for dimension in ("tenant", "provider", "run", "call", "demo_session")
    )
    for scope in scopes:
        service.set_limit(ctx, scope, "USD", cap, request_cap, synthetic=authority)
    store.commit()
    return ctx, connection.id, authority, scopes


def boundary_budget_cases(boundary_observer: BoundaryObserver) -> None:
    cases = [
        c
        for c in json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]
        if c["domain"] == "budget"
    ]
    seen = set()
    for case in cases:
        seen.add(case["id"])
        with actor_client("owner") as (client, store, user, tenant):
            value = case["input"]
            ctx, connection, authority, scopes = setup(
                store,
                tenant,
                user,
                Decimal(value.get("cap", value.get("money_cap", "0.10"))),
                value.get("request_cap", 100),
            )
            from aco.core.settings import FakePort

            assert isinstance(cast(FastAPI, client.app).state.ports.llm, FakePort)
            provider_transport_calls: list[dict[str, str]] = []

            # A fake-only registry has no real invocation implementation. Record attempts
            # made by this actual test executor, never a default DB counter.
            def execute_if_reserved(
                result: BudgetReservation, provider_transport_calls: Any = provider_transport_calls
            ) -> None:
                if result.status == "RESERVED":
                    provider_transport_calls.append({"mode": "fake", "operation": str(result.id)})

            if case["id"] == "budget_credentials_do_not_authorize":
                result = BudgetService(store).reserve_budget(
                    ctx,
                    UsageReservationRequest(
                        operation_key=str(uuid4()),
                        provider_connection_id=connection,
                        scope_ids=scopes,
                        currency="USD",
                        amount=Decimal(value["money_cap"]),
                        requests=value["request_cap"],
                    ),
                    authority=None,
                )
                store.commit()
                assert value["credential_configured"] is True
                assert cast(FastAPI, client.app).state.settings.identity_client_secret is not None
                assert value["external_authorization"] is None
                assert result.reason == "EXTERNAL_AUTHORIZATION_UNAVAILABLE"
                execute_if_reserved(result)
                assert provider_transport_calls == []
                actual = {"provider_request_allowed": result.status == "RESERVED", "status": result.status}
            elif case["id"] == "budget_concurrent_reservations_bounded":
                assert value["authorized_scope"] == "synthetic_test_only"
                assert Decimal(value["existing_reserved"]) == 0
                assert list(store.scalars(select(BudgetReservation))) == []
                store.commit()
                barrier = Barrier(2)

                def reserve(
                    amount: Any,
                    barrier: Any = barrier,
                    ctx: Any = ctx,
                    connection: Any = connection,
                    scopes: Any = scopes,
                    authority: Any = authority,
                ) -> Any:
                    with cast(FastAPI, client.app).state.sessions.begin() as separate:
                        barrier.wait(timeout=10)
                        result = BudgetService(separate).reserve_budget(
                            ctx,
                            UsageReservationRequest(
                                operation_key=str(uuid4()),
                                provider_connection_id=connection,
                                scope_ids=scopes,
                                currency="USD",
                                amount=Decimal(amount),
                                requests=1,
                            ),
                            authority=authority,
                        )
                        execute_if_reserved(result)
                        return result.status, result.reason

                with ThreadPoolExecutor(2) as pool:
                    statuses = list(pool.map(reserve, value["simultaneous_requests"]))
                assert sorted(status for status, reason in statuses) == ["BLOCKED", "RESERVED"]
                assert next(reason for status, reason in statuses if status == "BLOCKED").startswith(
                    "CAP_REACHED:"
                )
                rows = list(
                    store.scalars(select(BudgetReservation).where(BudgetReservation.tenant_id == tenant))
                )
                reserved = sum((r.retained_amount for r in rows), Decimal(0))
                actual = {
                    "accepted_reservations": sum(status == "RESERVED" for status, reason in statuses),
                    "reserved_max": str(reserved),
                    "cap_exceeded": reserved > Decimal(value["cap"]),
                    "real_provider_calls": sum(c["mode"] != "fake" for c in provider_transport_calls),
                }
            elif case["id"] == "budget_unknown_outcome_keeps_reservation":
                service = BudgetService(store)
                reservation = service.reserve_budget(
                    ctx,
                    UsageReservationRequest(
                        operation_key=str(uuid4()),
                        provider_connection_id=connection,
                        scope_ids=scopes,
                        currency="USD",
                        amount=Decimal(value["reserved"]),
                        requests=1,
                    ),
                    authority=authority,
                )
                service.reconcile_reservation(
                    ctx,
                    reservation.id,
                    UsageOutcome(
                        key="unknown", status=value["outcome"], verified_unused=value["verified_unused"]
                    ),
                )
                store.commit()
                store.refresh(reservation)
                actual = {
                    "released": str(reservation.released_amount),
                    "retained_reserved": str(reservation.retained_amount),
                    "automatic_retry": reservation.automatic_retry,
                }
            else:
                raise AssertionError("Unmapped budget case: " + case["id"])
            print(
                json.dumps(
                    {
                        "case": case["id"],
                        "tenant_id": str(tenant),
                        "connection_id": str(connection),
                        "actual": actual,
                        "reservations": [
                            str(row.id)
                            for row in store.scalars(
                                select(BudgetReservation).where(BudgetReservation.tenant_id == tenant)
                            )
                        ],
                        "transport_ledger": provider_transport_calls,
                    },
                    sort_keys=True,
                )
            )
            for key, expected in case["expected"].items():
                assert key in actual
                if key in {"released", "retained_reserved", "reserved_max"}:
                    assert Decimal(str(actual[key])) == Decimal(str(expected)), (case["id"], actual)
                else:
                    assert actual[key] == expected, (case["id"], actual)
            boundary_observer.assert_observed(
                case["id"],
                {
                    key: format(Decimal(str(value)).normalize(), "f")
                    if key in {"released", "retained_reserved", "reserved_max"}
                    else value
                    for key, value in actual.items()
                },
            )
    assert len(seen) == 3


def ledger_cases() -> None:
    with actor_client("owner") as (client, store, user, tenant):
        ctx, connection, authority, scopes = setup(store, tenant, user)
        service = UsageService(store)
        for index in range(3):
            event = ProviderUsageInput(
                provider_connection_id=connection,
                operation_key=f"tiny-{index}",
                component="model",
                operation="run_typed",
                provider_kind="llm",
                provider="mock",
                synthetic=True,
                cost=ProviderCost(
                    amount=Decimal("0.000000000001"), currency="USD", quality=CostQuality.PROVIDER_REPORTED
                ),
            )
            first = service.record_usage(ctx, event)
            assert service.record_usage(ctx, event).id == first.id
        store.commit()
        assert (
            len(list(store.scalars(select(ProviderUsageEvent).where(ProviderUsageEvent.tenant_id == tenant))))
            == 3
        )
        response = client.get(f"/api/workspaces/{tenant}/usage")
        assert response is not None
        assert response.status_code == 200, response.text
        assert Decimal(response.json()["currencies"][0]["known_amount"]) == Decimal("0.000000000003")


def concurrent_dimensions() -> None:
    """Every scope independently binds, including when other scopes have higher limits."""
    from aco.features.usage.models import BudgetLimit, BudgetScope

    for dimension in ("tenant", "provider", "run", "call", "demo_session"):
        with actor_client("owner") as (client, store, user, tenant):
            ctx, connection, authority, scopes = setup(store, tenant, user, Decimal("10"))
            selected = store.scalar(select(BudgetScope).where(BudgetScope.dimension == dimension))
            assert selected is not None
            limit = store.scalar(select(BudgetLimit).where(BudgetLimit.scope_id == selected.id))
            assert limit is not None
            limit.amount = Decimal("0.10")
            store.commit()
            barrier = Barrier(2)

            def reserve(
                _: Any,
                barrier: Any = barrier,
                ctx: Any = ctx,
                connection: Any = connection,
                scopes: Any = scopes,
                authority: Any = authority,
            ) -> Any:
                with cast(FastAPI, client.app).state.sessions.begin() as second:
                    barrier.wait(timeout=10)
                    result = BudgetService(second).reserve_budget(
                        ctx,
                        UsageReservationRequest(
                            operation_key=str(uuid4()),
                            provider_connection_id=connection,
                            scope_ids=scopes,
                            currency="USD",
                            amount=Decimal("0.06"),
                        ),
                        authority=authority,
                    )
                    return result.status, result.reason

            with ThreadPoolExecutor(2) as pool:
                outcomes = list(pool.map(reserve, range(2)))
            assert sorted(status for status, reason in outcomes) == ["BLOCKED", "RESERVED"]
            assert (
                next(reason for status, reason in outcomes if status == "BLOCKED")
                == f"CAP_REACHED:{dimension}"
            )


def duplicate_conflicts() -> None:
    import pytest
    from fastapi import HTTPException

    from aco.features.identity.models import now
    from aco.features.usage.models import RateSnapshot, UsageRevision

    with actor_client("owner") as (client, store, user, tenant):
        ctx, connection, authority, scopes = setup(store, tenant, user)
        service = UsageService(store)
        rate = RateSnapshot(
            tenant_id=tenant,
            provider="mock",
            currency="USD",
            unit="requests",
            amount=Decimal("0.05"),
            price_source_at=now(),
            synthetic=True,
        )
        store.add(rate)
        store.flush()
        event = ProviderUsageInput(
            provider_connection_id=connection,
            operation_key="one-call",
            price_source_at=rate.price_source_at,
            component="voice",
            operation="start_call",
            provider_kind="llm",
            provider="mock",
            synthetic=True,
            cost=ProviderCost(
                amount=Decimal("0.05"),
                currency="USD",
                quality=CostQuality.RATE_ESTIMATE,
                rate_snapshot_id=rate.id,
            ),
        )
        original = service.record_usage(ctx, event)
        with pytest.raises(HTTPException, match="USAGE_REPLAY_CONFLICT"):
            service.record_usage(
                ctx,
                event.model_copy(
                    update={
                        "cost": ProviderCost(
                            amount=Decimal("0.06"), currency="USD", quality=CostQuality.PROVIDER_REPORTED
                        )
                    }
                ),
            )
        actual = event.model_copy(
            update={
                "reconciliation_key": "actual",
                "cost": ProviderCost(
                    amount=Decimal("0.07"), currency="USD", quality=CostQuality.PROVIDER_REPORTED
                ),
            }
        )
        assert service.record_usage(ctx, actual).id == original.id
        assert service.record_usage(ctx, actual).id == original.id
        store.commit()
        revisions = list(
            store.scalars(
                select(UsageRevision)
                .where(UsageRevision.event_id == original.id)
                .order_by(UsageRevision.version)
            )
        )
        assert [r.cost_amount for r in revisions] == [Decimal("0.05"), Decimal("0.07")]
        summary = client.get(f"/api/workspaces/{tenant}/usage").json()
        assert Decimal(summary["currencies"][0]["known_amount"]) == Decimal("0.07")


def abandoned_reconciliation() -> None:
    with actor_client("owner") as (client, store, user, tenant):
        ctx, connection, authority, scopes = setup(store, tenant, user)
        service = BudgetService(store)
        row = service.reserve_budget(
            ctx,
            UsageReservationRequest(
                operation_key="abandoned",
                provider_connection_id=connection,
                scope_ids=scopes,
                currency="USD",
                amount=Decimal("0.06"),
            ),
            authority=authority,
        )
        service.reconcile_reservation(ctx, row.id, UsageOutcome(key="abandoned", status="ABANDONED"))
        store.commit()
        assert row.retained_amount == Decimal("0.06") and row.released_amount == 0
        assert row.automatic_retry is False
        result = UsageOutcome(key="no-effect", status="NO_EFFECT", verified_unused=True)
        service.reconcile_reservation(ctx, row.id, result)
        service.reconcile_reservation(ctx, row.id, result)
        store.commit()
        assert row.retained_amount == 0 and row.released_amount == Decimal("0.06")
