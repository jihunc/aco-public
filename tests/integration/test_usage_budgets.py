"""Real PostgreSQL budget and usage assertions; no external provider traffic."""

import pytest
from boundary_evidence import BoundaryObserver
from identity_support import actor_client


def test_usage_summary_is_real_authorized_api() -> None:
    with actor_client("viewer") as (client, store, user, tenant):
        response = client.get(f"/api/workspaces/{tenant}/usage")
        assert response is not None
        assert response.status_code == 200, response.text
        assert response.json()["external_execution"] == "BLOCKED"
        assert response.json()["currencies"] == []


def test_boundary_budget_authorization_and_reservations(boundary_observer: BoundaryObserver) -> None:
    from usage_support import boundary_budget_cases

    boundary_budget_cases(boundary_observer)


def test_subcent_costs_accumulate_without_rounding_to_zero() -> None:
    from usage_support import ledger_cases

    ledger_cases()


def test_concurrent_reservations_cannot_exceed_cap() -> None:
    from usage_support import concurrent_dimensions

    concurrent_dimensions()


def test_duplicate_operation_records_charge_once() -> None:
    from usage_support import duplicate_conflicts

    duplicate_conflicts()


def test_failed_and_abandoned_calls_reconcile_reservations() -> None:
    from usage_support import abandoned_reconciliation

    abandoned_reconciliation()


def test_same_tenant_cap_is_shared_across_connections() -> None:
    from decimal import Decimal

    from sqlalchemy import select
    from usage_support import setup

    from aco.features.usage.budgets import BudgetService
    from aco.features.usage.dto import UsageReservationRequest
    from aco.features.usage.models import BudgetScope

    with actor_client("owner") as (client, store, user, tenant):
        first_ctx, first_connection, first_authority, first_scopes = setup(
            store, tenant, user, cap=Decimal("100")
        )
        ctx, second_connection, authority, scopes = setup(store, tenant, user, cap=Decimal("100"))
        tenant_scopes = list(store.scalars(select(BudgetScope).where(BudgetScope.dimension == "tenant")))
        assert len(tenant_scopes) == 1
        service = BudgetService(store)
        service.set_limit(ctx, tenant_scopes[0].id, "USD", Decimal("0.10"), 100, synthetic=authority)
        one = service.reserve_budget(
            first_ctx,
            UsageReservationRequest(
                operation_key="first-provider",
                provider_connection_id=first_connection,
                scope_ids=first_scopes,
                currency="USD",
                amount=Decimal("0.06"),
            ),
            authority=first_authority,
        )
        two = service.reserve_budget(
            ctx,
            UsageReservationRequest(
                operation_key="second-provider",
                provider_connection_id=second_connection,
                scope_ids=scopes,
                currency="USD",
                amount=Decimal("0.06"),
            ),
            authority=authority,
        )
        assert one.status == "RESERVED"
        assert two.status == "BLOCKED" and two.reason == "CAP_REACHED:tenant"


def test_actual_over_bound_and_unit_caps_remain_enforced() -> None:
    from decimal import Decimal

    from usage_support import setup

    from aco.features.usage.budgets import BudgetService
    from aco.features.usage.dto import Units, UsageOutcome, UsageReservationRequest

    with actor_client("owner") as (client, store, user, tenant):
        ctx, connection, authority, scopes = setup(store, tenant, user)
        service = BudgetService(store)
        for scope in scopes:
            service.set_limit(
                ctx, scope, "USD", Decimal("0.10"), 100, synthetic=authority, unit_caps={"input_tokens": 10}
            )
        request = UsageReservationRequest(
            operation_key="bounded",
            provider_connection_id=connection,
            scope_ids=scopes,
            currency="USD",
            amount=Decimal("0.06"),
            units=Units(input_tokens=5),
        )
        row = service.reserve_budget(ctx, request, authority=authority)
        service.reconcile_reservation(
            ctx,
            row.id,
            UsageOutcome(
                key="verified",
                status="VERIFIED",
                verified_unused=True,
                actual_amount=Decimal("0.12"),
                units=Units(input_tokens=12),
            ),
        )
        store.commit()
        assert row.spent_amount == Decimal("0.12") and row.released_amount == 0
        next_row = service.reserve_budget(
            ctx, request.model_copy(update={"operation_key": "next"}), authority=authority
        )
        assert next_row.status == "BLOCKED" and next_row.reason.startswith("CAP_REACHED:")


def test_usage_api_rejects_foreign_revoked_and_forged_authority() -> None:
    from uuid import uuid4

    from sqlalchemy import select

    from aco.features.identity.models import Membership

    with actor_client("viewer") as (client, store, user, tenant):
        for query in ("role=owner", "tenant_id=" + str(uuid4()), "external_authorization=approved"):
            assert client.get(f"/api/workspaces/{tenant}/usage?{query}").status_code == 422
        assert client.get(f"/api/workspaces/{uuid4()}/usage").status_code == 403
        assert (
            client.put(
                f"/api/workspaces/{tenant}/usage/draft-limit",
                json={"currency": "USD", "amount": "1", "requests": 1},
            ).status_code
            == 403
        )
        member = store.scalar(
            select(Membership).where(Membership.tenant_id == tenant, Membership.user_id == user.id)
        )
        assert member is not None
        member.active = False
        store.commit()
        assert client.get(f"/api/workspaces/{tenant}/usage").status_code == 403


def test_actual_units_not_estimate_bound_later_requests() -> None:
    from decimal import Decimal

    from usage_support import setup

    from aco.features.usage.budgets import BudgetService
    from aco.features.usage.dto import Units, UsageOutcome, UsageReservationRequest

    with actor_client("owner") as (client, store, user, tenant):
        ctx, connection, authority, scopes = setup(store, tenant, user, cap=Decimal("100"))
        service = BudgetService(store)
        for scope in scopes:
            service.set_limit(
                ctx,
                scope,
                "USD",
                Decimal("100"),
                100,
                synthetic=authority,
                unit_caps={"input_tokens": 10, "requests": 100},
            )
        request = UsageReservationRequest(
            operation_key="actual-units",
            provider_connection_id=connection,
            scope_ids=scopes,
            currency="USD",
            amount=Decimal("0.01"),
            units=Units(input_tokens=1),
        )
        row = service.reserve_budget(ctx, request, authority=authority)
        service.reconcile_reservation(
            ctx,
            row.id,
            UsageOutcome(
                key="actual",
                status="VERIFIED",
                verified_unused=True,
                actual_amount=Decimal("0.01"),
                units=Units(input_tokens=11),
            ),
        )
        store.commit()
        next_row = service.reserve_budget(
            ctx, request.model_copy(update={"operation_key": "next-unit"}), authority=authority
        )
        assert next_row.status == "BLOCKED" and next_row.reason.startswith("UNIT_CAP_REACHED:")


def test_unconfigured_unit_cap_is_zero_and_float_wire_is_rejected() -> None:
    from decimal import Decimal

    from usage_support import setup

    from aco.features.usage.budgets import BudgetService
    from aco.features.usage.dto import Units, UsageReservationRequest

    with actor_client("owner") as (client, store, user, tenant):
        ctx, connection, authority, scopes = setup(store, tenant, user)
        row = BudgetService(store).reserve_budget(
            ctx,
            UsageReservationRequest(
                operation_key="uncapped-token",
                provider_connection_id=connection,
                scope_ids=scopes,
                currency="USD",
                amount=Decimal("0.01"),
                units=Units(input_tokens=1),
            ),
            authority=authority,
        )
        assert row.status == "BLOCKED" and row.reason.startswith("UNIT_CAP_REACHED:")
        store.rollback()
        assert (
            client.put(
                f"/api/workspaces/{tenant}/usage/draft-limit",
                json={"currency": "USD", "amount": 0.1, "requests": 1},
            ).status_code
            == 422
        )


@pytest.mark.parametrize(
    "currency,amount",
    [
        ("ZZZ", "1"),
        ("USD", ""),
        ("USD", "not-money"),
        ("USD", "NaN"),
        ("USD", "Infinity"),
        ("USD", "100000000000000000000000000"),
        ("USD", "0.0000000000001"),
        ("USD", 0.1),
    ],
)
def test_invalid_draft_values_are_422_without_persisted_limits(currency: str, amount: str | float) -> None:
    from sqlalchemy import func, select

    from aco.features.usage.models import BudgetLimit, BudgetScope

    with actor_client("owner") as (client, store, user, tenant):
        before = (
            store.scalar(select(func.count()).select_from(BudgetLimit)),
            store.scalar(select(func.count()).select_from(BudgetScope)),
        )
        response = client.put(
            f"/api/workspaces/{tenant}/usage/draft-limit",
            json={"currency": currency, "amount": amount, "requests": 0},
        )
        assert response.status_code == 422, (currency, amount, response.status_code)
        assert before == (
            store.scalar(select(func.count()).select_from(BudgetLimit)),
            store.scalar(select(func.count()).select_from(BudgetScope)),
        )


@pytest.mark.parametrize("amount", ["0.10", "0.000000000003", "99999999999999999999999999.123456789012"])
def test_saved_draft_response_matches_independent_persisted_read(amount: str) -> None:
    from decimal import Decimal

    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from aco.features.usage.models import BudgetLimit

    with actor_client("owner") as (client, store, user, tenant):
        response = client.put(
            f"/api/workspaces/{tenant}/usage/draft-limit",
            json={"currency": "USD", "amount": amount, "requests": 0},
        )
        assert response.status_code == 200, response.text
        immediate = response.json()["limits"]
        reread = client.get(f"/api/workspaces/{tenant}/usage").json()["limits"]
        with Session(store.get_bind()) as independent:
            saved = independent.scalar(select(BudgetLimit).where(BudgetLimit.tenant_id == tenant))
            assert saved is not None and saved.amount == Decimal(amount)
            assert immediate == reread
            assert immediate[0]["amount"] == format(saved.amount, "f")
            assert immediate[0]["state"] == "DRAFT"


@pytest.mark.parametrize("bound", ["money", "requests", "messages"])
def test_currency_money_is_separate_but_quantity_caps_are_scope_wide(bound: str) -> None:
    from decimal import Decimal

    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from usage_support import setup

    from aco.features.usage.budgets import BudgetService
    from aco.features.usage.dto import Units, UsageReservationRequest
    from aco.features.usage.models import BudgetReservation

    with actor_client("owner") as (client, store, user, tenant):
        ctx, connection, authority, scopes = setup(store, tenant, user)
        budget = BudgetService(store)
        for scope in scopes:
            for currency in ("USD", "AUD"):
                budget.set_limit(
                    ctx,
                    scope,
                    currency,
                    Decimal("0.10") if bound == "money" else Decimal("100"),
                    1 if bound == "requests" and currency == "USD" else 100,
                    synthetic=authority,
                    unit_caps={"messages": 1 if bound == "messages" and currency == "USD" else 100},
                )
        usd = UsageReservationRequest(
            provider_connection_id=connection,
            scope_ids=scopes,
            operation_key="usd",
            currency="USD",
            amount=Decimal("0.06"),
            units=Units(messages=1 if bound == "messages" else 0),
        )
        first = budget.reserve_budget(ctx, usd, authority=authority)
        second = budget.reserve_budget(
            ctx, usd.model_copy(update={"operation_key": "aud", "currency": "AUD"}), authority=authority
        )
        assert first.status == "RESERVED"
        if bound == "money":
            assert second.status == "RESERVED"
            third = budget.reserve_budget(
                ctx, usd.model_copy(update={"operation_key": "usd-again"}), authority=authority
            )
            assert third.status == "BLOCKED" and third.reason.startswith("CAP_REACHED:")
        else:
            assert second.status == "BLOCKED"
            assert second.reason.startswith("UNIT_CAP_REACHED:" if bound == "messages" else "CAP_REACHED:")
            if bound == "messages":
                assert second.reason.endswith(":messages")
        store.commit()
        with Session(store.get_bind()) as independent:
            rows = list(independent.scalars(select(BudgetReservation)))
            amounts = {
                currency: sum(
                    (r.retained_amount + r.spent_amount for r in rows if r.currency == currency), Decimal(0)
                )
                for currency in ("USD", "AUD")
            }
            assert amounts == {
                "USD": Decimal("0.06"),
                "AUD": Decimal("0.06") if bound == "money" else Decimal(0),
            }
            print("F2 persisted", tenant, bound, amounts, [(r.status, r.reason) for r in rows])


@pytest.mark.parametrize("quantity_cap", [False, True])
def test_cross_currency_concurrent_admission_preserves_money_and_scope_units(quantity_cap: bool) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from decimal import Decimal
    from threading import Barrier

    from sqlalchemy import select, text
    from sqlalchemy.orm import Session
    from usage_support import setup

    from aco.features.usage.budgets import BudgetService
    from aco.features.usage.dto import UsageReservationRequest
    from aco.features.usage.models import BudgetReservation

    with actor_client("owner") as (client, store, user, tenant):
        ctx, connection, authority, scopes = setup(store, tenant, user)
        for scope in scopes:
            for currency in ("USD", "AUD"):
                BudgetService(store).set_limit(
                    ctx,
                    scope,
                    currency,
                    Decimal("0.10"),
                    1 if quantity_cap and currency == "USD" else 100,
                    synthetic=authority,
                )
        store.commit()
        barrier = Barrier(2)

        def reserve(currency: str) -> tuple[int, str, str]:
            with Session(store.get_bind()) as db:
                pid = db.scalar(text("select pg_backend_pid()"))
                barrier.wait(timeout=10)
                row = BudgetService(db).reserve_budget(
                    ctx,
                    UsageReservationRequest(
                        provider_connection_id=connection,
                        scope_ids=scopes,
                        operation_key=currency,
                        currency=currency,
                        amount=Decimal("0.06"),
                    ),
                    authority=authority,
                )
                result = (int(pid), row.status, row.reason)
                db.commit()
                return result

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(reserve, ("USD", "AUD")))
        assert len({pid for pid, status, reason in results}) == 2
        assert sorted(status for pid, status, reason in results) == (
            ["BLOCKED", "RESERVED"] if quantity_cap else ["RESERVED", "RESERVED"]
        )
        with Session(store.get_bind()) as verify:
            rows = list(
                verify.scalars(select(BudgetReservation).where(BudgetReservation.tenant_id == tenant))
            )
            assert sum(int(str(r.request["requests"])) for r in rows if r.status == "RESERVED") == (
                1 if quantity_cap else 2
            )
            assert all(r.retained_amount == Decimal("0.06") for r in rows if r.status == "RESERVED")
            print(
                "F2 concurrent",
                tenant,
                quantity_cap,
                results,
                [(r.currency, r.retained_amount) for r in rows],
            )


def test_concurrent_other_currency_cap_change_is_current_before_admission() -> None:
    from concurrent.futures import ThreadPoolExecutor
    from decimal import Decimal
    from threading import Event
    from time import monotonic, sleep

    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from usage_support import setup

    from aco.features.usage.budgets import BudgetService
    from aco.features.usage.dto import UsageReservationRequest

    with actor_client("owner") as (client, store, user, tenant):
        ctx, connection, authority, scopes = setup(store, tenant, user)
        for scope in scopes:
            BudgetService(store).set_limit(ctx, scope, "AUD", Decimal("0.10"), 100, synthetic=authority)
        store.commit()
        BudgetService(store).set_limit(ctx, scopes[0], "USD", Decimal("0.10"), 0, synthetic=authority)
        started = Event()
        worker_pid: list[int] = []

        def reserve() -> tuple[str, str]:
            with Session(store.get_bind()) as db:
                worker_pid.append(int(db.scalar(text("select pg_backend_pid()"))))
                started.set()
                row = BudgetService(db).reserve_budget(
                    ctx,
                    UsageReservationRequest(
                        provider_connection_id=connection,
                        scope_ids=scopes,
                        operation_key="after-cap-change",
                        currency="AUD",
                        amount=Decimal("0.06"),
                    ),
                    authority=authority,
                )
                result = row.status, row.reason
                db.commit()
                return result

        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(reserve)
            assert started.wait(timeout=10)
            with Session(store.get_bind()) as observer:
                deadline = monotonic() + 5
                while monotonic() < deadline:
                    blocked = observer.scalar(
                        text("select cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": worker_pid[0]}
                    )
                    if blocked:
                        break
                    sleep(0.01)
                assert blocked is True and not future.done()
            store.commit()
            result = future.result(timeout=10)
        assert result == ("BLOCKED", "CAP_REACHED:tenant")
        print("F2 cap update lock", tenant, worker_pid, result)
