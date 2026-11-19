"""Actual LIVE-code scope aggregation across replaced connections; no external requests."""

from decimal import Decimal
from uuid import UUID, uuid4

from action_support import action_world
from integration.test_live_tenant_ceiling import reviewed_command
from live_tenant_support import ai_grant

from aco.features.identity.models import ProviderConnection
from aco.features.usage.budgets import BudgetService
from aco.features.usage.dto import Units, UsageOutcome, UsageReservationRequest
from aco.features.usage.live_tenant import review_live_tenant_budget


def test_replaced_connection_cannot_reset_same_vendor_request_cap() -> None:
    with action_world() as world:
        review_live_tenant_budget(world.store, world.ctx, reviewed_command())
        first = ai_grant(world)
        statuses = []
        for index in range(4):
            if index == 3:
                old = world.store.get(ProviderConnection, first.provider_connection_id)
                assert old is not None
                old.status = "DISCONNECTED"
                old.version += 1
                world.store.flush()
                grant = ai_grant(world)
                assert first.provider_connection_id != grant.provider_connection_id
            else:
                grant = first
            result = BudgetService(world.store)._reserve_checked(
                world.ctx,
                UsageReservationRequest(
                    operation_key="replaced-connection:" + str(uuid4()),
                    provider_connection_id=grant.provider_connection_id,
                    scope_ids=tuple(UUID(key) for key in grant.scope_ids),
                    currency="USD",
                    amount=Decimal(".01"),
                    units=Units(input_tokens=100),
                ),
                allowed=True,
                limit_state="BOUNDED_LIVE_TEST",
            )
            statuses.append((result.status, result.reason))
            if index == 0:
                BudgetService(world.store).reconcile_reservation(
                    world.ctx,
                    result.id,
                    UsageOutcome(key="retained-unknown", status="UNKNOWN_SIDE_EFFECT"),
                )
                assert result.retained_amount == Decimal(".01")
        assert [status for status, _ in statuses] == ["RESERVED"] * 3 + ["BLOCKED"]
        assert statuses[-1][1] == "CAP_REACHED:provider"


def test_ambiguous_historical_vendor_scopes_refuse_without_resetting_exposure() -> None:
    import pytest
    from fastapi import HTTPException
    from sqlalchemy import select

    from aco.features.usage.models import BudgetScope, RateSnapshot

    with action_world() as world:
        ai_grant(world)
        anchor = ProviderConnection(
            tenant_id=world.ctx.tenant_id,
            provider="openai",
            provider_type="llm",
            status="DISCONNECTED",
        )
        world.store.add(anchor)
        world.store.flush()
        world.store.add(
            BudgetScope(
                tenant_id=world.ctx.tenant_id,
                provider_connection_id=anchor.id,
                dimension="provider",
                synthetic=False,
                creator_id=world.ctx.actor_id,
            )
        )
        world.store.flush()
        scope_ids = set(world.store.scalars(select(BudgetScope.id)))
        rate_ids = set(world.store.scalars(select(RateSnapshot.id)))
        with pytest.raises(HTTPException, match="LIVE_PROVIDER_SCOPE_AMBIGUOUS"):
            with world.store.begin_nested():
                ai_grant(world)
        assert set(world.store.scalars(select(BudgetScope.id))) == scope_ids
        assert set(world.store.scalars(select(RateSnapshot.id))) == rate_ids


def test_provider_lookup_preserves_tenant_and_vendor_kind_boundaries() -> None:
    import pytest
    from fastapi import HTTPException

    from aco.features.identity.models import Tenant
    from aco.features.usage.live_scopes import live_provider_scope
    from aco.features.usage.models import BudgetScope

    with action_world() as world:
        grant = ai_grant(world)
        own = world.store.get(ProviderConnection, grant.provider_connection_id)
        assert own is not None
        own_scope = live_provider_scope(world.store, world.ctx.tenant_id, own)
        assert own_scope is not None
        other = Tenant(name="Other synthetic company", timezone="UTC")
        world.store.add(other)
        world.store.flush()
        foreign = ProviderConnection(
            tenant_id=other.id, provider="openai", provider_type="llm", status="READY"
        )
        other_kind = ProviderConnection(
            tenant_id=world.ctx.tenant_id, provider="openai", provider_type="voice", status="READY"
        )
        world.store.add_all([foreign, other_kind])
        world.store.flush()
        world.store.add(
            BudgetScope(
                tenant_id=other.id,
                provider_connection_id=foreign.id,
                dimension="provider",
                synthetic=False,
                creator_id=world.ctx.actor_id,
            )
        )
        world.store.flush()
        retained = live_provider_scope(world.store, world.ctx.tenant_id, own)
        assert retained is not None and retained.id == own_scope.id
        assert live_provider_scope(world.store, world.ctx.tenant_id, other_kind) is None
        with pytest.raises(HTTPException, match="FOREIGN_SCOPE"):
            live_provider_scope(world.store, world.ctx.tenant_id, foreign)
