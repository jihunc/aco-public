"""Provider limits aggregate vendor identity; owner limits cannot grant live authority."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from identity_support import actor_client
from sqlalchemy import select
from usage_support import setup

from aco.features.usage.budgets import BudgetService
from aco.features.usage.dto import UsageReservationRequest
from aco.features.usage.models import BudgetScope


def test_provider_cap_spans_same_vendor_connections_with_nonbinding_tenant() -> None:
    with actor_client("owner") as (client, store, user, tenant):
        ctx, first, first_authority, first_scopes = setup(store, tenant, user, cap=Decimal("100"))
        ctx, second, second_authority, second_scopes = setup(store, tenant, user, cap=Decimal("100"))
        service = BudgetService(store)
        providers = list(store.scalars(select(BudgetScope).where(BudgetScope.dimension == "provider")))
        assert len(providers) == 1
        service.set_limit(ctx, providers[0].id, "USD", Decimal("0.10"), 100, synthetic=first_authority)
        store.commit()
        barrier = Barrier(2)

        def reserve(args: Any) -> tuple[str, str]:
            connection, scopes, authority = args
            barrier.wait(timeout=5)
            with cast(FastAPI, client.app).state.sessions.begin() as session:
                result = BudgetService(session).reserve_budget(
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
            results = list(
                pool.map(
                    reserve,
                    ((first, first_scopes, first_authority), (second, second_scopes, second_authority)),
                )
            )
        assert sorted(status for status, reason in results) == ["BLOCKED", "RESERVED"]
        assert next(reason for status, reason in results if status == "BLOCKED") == "CAP_REACHED:provider"


def test_different_providers_still_share_tenant_cap() -> None:
    from aco.features.identity.models import ProviderConnection

    with actor_client("owner") as (client, store, user, tenant):
        ctx, first, first_authority, first_scopes = setup(store, tenant, user)
        second = ProviderConnection(tenant_id=tenant, provider="demo", provider_type="llm", status="READY")
        store.add(second)
        store.commit()
        service = BudgetService(store)
        authority = service.synthetic_authority(ctx, second.id, "local")
        scopes = tuple(
            service.create_scope(ctx, authority, d).id
            for d in ("tenant", "provider", "run", "call", "demo_session")
        )
        for key in scopes:
            scope = store.get(BudgetScope, key)
            assert scope is not None
            dimension = scope.dimension
            service.set_limit(
                ctx,
                key,
                "USD",
                Decimal("0.10") if dimension == "tenant" else Decimal("100"),
                100,
                synthetic=authority,
            )
        one = service.reserve_budget(
            ctx,
            UsageReservationRequest(
                operation_key="first-vendor",
                provider_connection_id=first,
                scope_ids=first_scopes,
                currency="USD",
                amount=Decimal("0.06"),
            ),
            authority=first_authority,
        )
        two = service.reserve_budget(
            ctx,
            UsageReservationRequest(
                operation_key="second-vendor",
                provider_connection_id=second.id,
                scope_ids=scopes,
                currency="USD",
                amount=Decimal("0.06"),
            ),
            authority=authority,
        )
        assert one.status == "RESERVED" and two.status == "BLOCKED"
        assert two.reason == "CAP_REACHED:tenant"


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "not_demo",
        "connection_blocked",
        "provider_changed",
        "scope_changed",
        "lowered_cap",
        "membership_revoked",
    ],
)
def test_reservation_replay_rechecks_current_authority_without_releasing_original(change: str) -> None:
    import pytest
    from fastapi import HTTPException
    from sqlalchemy import func

    from aco.features.identity.models import Membership, ProviderConnection, Tenant
    from aco.features.usage.models import BudgetLimit, BudgetReservation, ReservationOutcome

    with actor_client("owner") as (client, store, user, tenant):
        ctx, connection, authority, scopes = setup(store, tenant, user)
        source = store.get(ProviderConnection, connection)
        assert source is not None
        source.status = "READY"
        service = BudgetService(store)
        request = UsageReservationRequest(
            operation_key="replay-admission",
            provider_connection_id=connection,
            scope_ids=scopes,
            currency="USD",
            amount=Decimal("0.06"),
        )
        original = service.reserve_budget(ctx, request, authority=authority)
        store.commit()
        if change == "not_demo":
            workspace = store.get(Tenant, tenant)
            assert workspace is not None
            workspace.is_demo = False
        elif change == "connection_blocked":
            source.status = "BLOCKED"
        elif change == "provider_changed":
            source.provider = "future-real-provider"
        elif change == "scope_changed":
            other = ProviderConnection(tenant_id=tenant, provider="mock", provider_type="llm", status="READY")
            store.add(other)
            store.flush()
            scope = store.scalar(select(BudgetScope).where(BudgetScope.dimension == "run"))
            assert scope is not None
            scope.provider_connection_id = other.id
        elif change == "lowered_cap":
            limit = store.scalar(
                select(BudgetLimit).join(BudgetScope).where(BudgetScope.dimension == "tenant")
            )
            assert limit is not None
            limit.amount = Decimal("0.01")
        elif change == "membership_revoked":
            member = store.scalar(
                select(Membership).where(Membership.tenant_id == tenant, Membership.user_id == user.id)
            )
            assert member is not None
            member.active = False
        store.commit()
        with pytest.raises(HTTPException) as failure:
            service.reserve_budget(ctx, request, authority=None if change == "missing" else authority)
        assert failure.value.status_code == 403
        store.expire_all()
        retained = store.get(BudgetReservation, original.id)
        assert retained is not None and retained.status == "RESERVED"
        assert retained.retained_amount == Decimal("0.06") and retained.released_amount == 0
        assert store.scalar(select(func.count()).select_from(BudgetReservation)) == 1
        assert store.scalar(select(func.count()).select_from(ReservationOutcome)) == 0


def test_authorized_replay_retains_one_admission_and_honors_concurrent_cap_change() -> None:
    from fastapi import HTTPException

    from aco.features.usage.models import BudgetLimit

    with actor_client("owner") as (client, store, user, tenant):
        ctx, connection, authority, scopes = setup(store, tenant, user)
        service = BudgetService(store)
        request = UsageReservationRequest(
            operation_key="valid-replay",
            provider_connection_id=connection,
            scope_ids=scopes,
            currency="USD",
            amount=Decimal("0.06"),
        )
        original = service.reserve_budget(ctx, request, authority=authority)
        store.commit()
        assert service.reserve_budget(ctx, request, authority=authority).id == original.id
        store.commit()
        # Keep this Session's old limit object loaded while a second transaction lowers it.
        limit = store.scalar(select(BudgetLimit).join(BudgetScope).where(BudgetScope.dimension == "tenant"))
        assert limit is not None and limit.amount == Decimal("0.10")
        with cast(FastAPI, client.app).state.sessions.begin() as other:
            changed = other.get(BudgetLimit, limit.id)
            assert changed is not None
            changed.amount = Decimal("0.01")
        with pytest.raises(HTTPException, match="CAP_REACHED:tenant"):
            service.reserve_budget(ctx, request, authority=authority)
        assert original.retained_amount == Decimal("0.06") and original.status == "RESERVED"


@pytest.mark.parametrize("action", ["admit", "terminal"])
def test_retained_orm_reservation_uses_independently_committed_financial_state(action: str) -> None:
    from fastapi import HTTPException
    from sqlalchemy.orm import Session

    from aco.features.usage.dto import UsageOutcome
    from aco.features.usage.models import BudgetReservation, ReservationOutcome

    with actor_client("owner") as (client, store, user, tenant):
        ctx, connection, authority, scopes = setup(store, tenant, user)
        request = UsageReservationRequest(
            provider_connection_id=connection,
            scope_ids=scopes,
            operation_key="first",
            currency="USD",
            amount=Decimal("0.06"),
        )
        service = BudgetService(store)
        first = service.reserve_budget(ctx, request, authority=authority)
        key = first.id
        store.commit()
        retained = store.get(BudgetReservation, key)
        assert retained is not None and retained.retained_amount == Decimal("0.06")
        with Session(store.get_bind()) as other:
            BudgetService(other).reconcile_reservation(
                ctx,
                key,
                UsageOutcome(
                    key="reported", status="VERIFIED", verified_unused=True, actual_amount=Decimal("0.09")
                ),
            )
            other.commit()
        assert retained.status == "RESERVED"  # real held identity-map object, not a fresh Session
        if action == "admit":
            second = service.reserve_budget(
                ctx,
                request.model_copy(update={"operation_key": "second", "amount": Decimal("0.04")}),
                authority=authority,
            )
            assert second.status == "BLOCKED" and second.reason.startswith("CAP_REACHED:")
        else:
            with pytest.raises(HTTPException) as error:
                service.reconcile_reservation(
                    ctx, key, UsageOutcome(key="late-no-effect", status="NO_EFFECT", verified_unused=True)
                )
            assert error.value.status_code == 409 and error.value.detail == "RESERVATION_TERMINAL"
        store.commit()
        with Session(store.get_bind()) as verify:
            current = verify.get(BudgetReservation, key)
            assert current is not None and current.status == "VERIFIED"
            assert current.spent_amount == Decimal("0.09")
            assert current.retained_amount == current.released_amount == 0
            outcomes = list(verify.scalars(select(ReservationOutcome)))
            assert len(outcomes) == 1 and outcomes[0].outcome_key == "reported"
            exposure = sum(
                (r.retained_amount + r.spent_amount for r in verify.scalars(select(BudgetReservation))),
                Decimal(0),
            )
            assert exposure == Decimal("0.09")
            print("F1 persisted", tenant, key, action, current.status, exposure, len(outcomes))
