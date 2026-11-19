"""Current operator-reviewed canonical tenant ceiling; no provider requests."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from threading import Barrier
from typing import cast
from uuid import UUID, uuid4

import pytest
from action_support import action_world
from fastapi import HTTPException
from live_tenant_support import ai_grant, email_grant
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.live import DELIVERY_LIVE_AUTHORIZATION
from aco.features.usage.live_tenant import (
    CompleteLiveUnits,
    ReviewLiveTenantBudget,
    review_live_tenant_budget,
)
from aco.features.usage.models import BudgetLimit, BudgetScope


@pytest.mark.parametrize("order", ["ai_first", "email_first"])
def test_provider_installation_never_rewrites_existing_tenant_ceiling(
    order: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACO_DELIVERY_APPROVED_EMAIL_RECIPIENT", "delivery-recipient@synthetic.test")
    with action_world() as world:
        first, second = (ai_grant, email_grant) if order == "ai_first" else (email_grant, ai_grant)
        first(world)
        world.store.flush()
        limit = world.store.scalar(
            select(BudgetLimit)
            .join(BudgetScope)
            .where(
                BudgetScope.tenant_id == world.ctx.tenant_id,
                BudgetScope.dimension == "tenant",
                BudgetLimit.state == "BOUNDED_LIVE_TEST",
            )
        )
        assert limit is not None
        before = (limit.amount, limit.requests, dict(limit.unit_caps), limit.version)
        second(world)
        assert (limit.amount, limit.requests, dict(limit.unit_caps), limit.version) == before


@pytest.mark.parametrize("order", ["ai_first", "email_first"])
def test_explicit_operator_ceiling_preserves_unknown_exposure_and_current_version(
    order: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACO_DELIVERY_APPROVED_EMAIL_RECIPIENT", "delivery-recipient@synthetic.test")
    from aco.features.identity.models import AuditEvent
    from aco.features.usage.budgets import BudgetService
    from aco.features.usage.dto import Units, UsageOutcome, UsageReservationRequest
    from aco.features.usage.live_tenant import review_live_tenant_budget

    with action_world() as world:
        command = reviewed_command()
        limit = review_live_tenant_budget(world.store, world.ctx, command)
        if order == "ai_first":
            ai, email = ai_grant(world), email_grant(world)
        else:
            email, ai = email_grant(world), ai_grant(world)
        world.store.flush()
        before = (limit.amount, limit.requests, dict(limit.unit_caps))
        for grant, connection, units in (
            (ai, ai.provider_connection_id, Units(input_tokens=100, output_tokens=20)),
            (email, email.connection_id, Units(messages=1)),
        ):
            result = BudgetService(world.store)._reserve_checked(
                world.ctx,
                UsageReservationRequest(
                    operation_key="offline:" + str(uuid4()),
                    provider_connection_id=connection,
                    scope_ids=tuple(UUID(key) for key in grant.scope_ids),
                    currency="USD",
                    amount=Decimal(".01"),
                    units=units,
                ),
                allowed=True,
                limit_state="BOUNDED_LIVE_TEST",
            )
            assert result.status == "RESERVED", result.reason
            BudgetService(world.store).reconcile_reservation(
                world.ctx, result.id, UsageOutcome(key="unknown", status="UNKNOWN_SIDE_EFFECT")
            )
            assert result.status == "UNKNOWN_SIDE_EFFECT" and result.retained_amount == Decimal(".01")
        world.store.flush()
        smaller = command.model_copy(
            update={
                "expected_limit_id": limit.id,
                "expected_version": limit.version,
                "amount": Decimal(".01"),
            }
        )
        with pytest.raises(HTTPException, match="TENANT_LIMIT_BELOW_EXPOSURE"):
            review_live_tenant_budget(world.store, world.ctx, smaller)
        assert (limit.amount, limit.requests, dict(limit.unit_caps)) == before
        updated = review_live_tenant_budget(
            world.store,
            world.ctx,
            command.model_copy(
                update={
                    "expected_limit_id": limit.id,
                    "expected_version": limit.version,
                    "amount": Decimal("3"),
                }
            ),
        )
        assert updated.version == 2
        with pytest.raises(HTTPException, match="TENANT_LIMIT_VERSION_CONFLICT"):
            review_live_tenant_budget(world.store, world.ctx, smaller)
        events = list(
            world.store.scalars(select(AuditEvent).where(AuditEvent.action == "budget.live_tenant_reviewed"))
        )
        assert len(events) == 2
        details = json.loads(json.dumps(events[-1].details))
        assert Decimal(details["prior"]["amount"]) == 2
        assert Decimal(details["exposure"]["amount"]) == Decimal(".02")


def reviewed_command(requests: int = 6) -> ReviewLiveTenantBudget:
    return ReviewLiveTenantBudget(
        authorization_ref=DELIVERY_LIVE_AUTHORIZATION,
        expected_limit_id=None,
        expected_version=None,
        amount=Decimal("2"),
        requests=requests,
        units=CompleteLiveUnits(
            requests=requests,
            input_tokens=4096,
            output_tokens=1024,
            cached_tokens=4096,
            messages=2,
            duration_seconds=Decimal("300"),
        ),
        review_reason="Explicit isolated operator cross-provider review",
    )


def test_concurrent_installers_and_cross_provider_reservations_share_one_tenant_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ACO_DELIVERY_APPROVED_EMAIL_RECIPIENT", "delivery-recipient@synthetic.test")
    from aco.features.usage.budgets import BudgetService
    from aco.features.usage.dto import Units, UsageReservationRequest
    from aco.features.usage.models import BudgetReservation

    with action_world() as world:
        limit = review_live_tenant_budget(world.store, world.ctx, reviewed_command(requests=1))
        limit_id = limit.id
        engine = world.store.get_bind()
        world.store.commit()
        barrier = Barrier(2)

        def install(provider: str) -> tuple[UUID, tuple[UUID, ...]]:
            with Session(engine) as db, db.begin():
                lane = replace(world, store=db)
                barrier.wait(timeout=10)
                if provider == "ai":
                    grant = ai_grant(lane)
                    connection_id, scope_ids = grant.provider_connection_id, grant.scope_ids
                else:
                    mail = email_grant(lane)
                    connection_id, scope_ids = mail.connection_id, mail.scope_ids
                return connection_id, tuple(UUID(key) for key in scope_ids)

        with ThreadPoolExecutor(2) as pool:
            grants = list(pool.map(install, ["ai", "email"]))
        current = world.store.get(BudgetLimit, limit_id, populate_existing=True)
        assert current is not None and current.requests == 1 and current.amount == 2
        assert current.version == 1 and current.unit_caps == reviewed_command(1).units.model_dump(mode="json")
        world.store.rollback()
        barrier = Barrier(2)

        def reserve(index: int) -> str:
            connection, scopes = grants[index]
            with Session(engine) as db, db.begin():
                barrier.wait(timeout=10)
                row = BudgetService(db)._reserve_checked(
                    world.ctx,
                    UsageReservationRequest(
                        operation_key="concurrent:" + str(uuid4()),
                        provider_connection_id=connection,
                        scope_ids=scopes,
                        currency="USD",
                        amount=Decimal(".01"),
                        units=Units(input_tokens=10) if index == 0 else Units(messages=1),
                    ),
                    allowed=True,
                    limit_state="BOUNDED_LIVE_TEST",
                )
                return row.status

        with ThreadPoolExecutor(2) as pool:
            statuses = list(pool.map(reserve, [0, 1]))
        assert sorted(statuses) == ["BLOCKED", "RESERVED"]
        rows = list(world.store.scalars(select(BudgetReservation)))
        assert len(rows) == 2 and sum(row.retained_amount for row in rows) == Decimal(".01")


def test_operator_ceiling_rejects_stale_membership_foreign_scope_and_legacy_authority() -> None:
    from aco.core.context import ServiceContext, TenantContext
    from aco.features.identity.models import AuditEvent, Membership, Tenant

    with action_world() as world:
        command = reviewed_command()
        member = world.store.scalar(
            select(Membership).where(
                Membership.tenant_id == world.ctx.tenant_id,
                Membership.user_id == world.ctx.actor_id,
            )
        )
        assert member is not None
        for role in ("finance_manager", "operator", "viewer"):
            member.role = role
            world.store.flush()
            with pytest.raises(HTTPException) as refusal:
                review_live_tenant_budget(world.store, world.ctx, command)
            assert refusal.value.status_code == 403
        member.role, member.active = "owner", False
        world.store.flush()
        with pytest.raises(HTTPException) as refusal:
            review_live_tenant_budget(world.store, world.ctx, command)
        assert refusal.value.status_code == 403
        member.active = True
        world.store.flush()
        with pytest.raises(HTTPException, match="CURRENT_TENANT_LIVE_REVIEW_REQUIRED"):
            review_live_tenant_budget(
                world.store,
                world.ctx,
                command.model_copy(update={"authorization_ref": "user-bounded-provider-smoke-2026-11-18"}),
            )
        with pytest.raises(HTTPException) as refusal:
            review_live_tenant_budget(
                world.store, world.ctx.model_copy(update={"tenant_id": uuid4()}), command
            )
        assert refusal.value.status_code == 403
        service = ServiceContext(
            tenant_id=world.ctx.tenant_id,
            actor_id=world.ctx.actor_id,
            correlation_id=uuid4(),
            action_id=uuid4(),
            action_version=1,
        )
        with pytest.raises(HTTPException, match="HUMAN_CONTEXT_REQUIRED"):
            review_live_tenant_budget(world.store, cast(TenantContext, service), command)
        tenant = world.store.get(Tenant, world.ctx.tenant_id)
        assert tenant is not None
        tenant.is_demo = False
        world.store.flush()
        with pytest.raises(HTTPException, match="CURRENT_TENANT_LIVE_REVIEW_REQUIRED"):
            review_live_tenant_budget(world.store, world.ctx, command)
        assert (
            list(
                world.store.scalars(
                    select(AuditEvent).where(AuditEvent.action == "budget.live_tenant_reviewed")
                )
            )
            == []
        )
        assert (
            list(world.store.scalars(select(BudgetLimit).where(BudgetLimit.state == "BOUNDED_LIVE_TEST")))
            == []
        )


def test_review_requires_complete_finite_caps_within_current_directive() -> None:
    from pydantic import ValidationError

    base = reviewed_command().model_dump(mode="json")
    for key in CompleteLiveUnits.model_fields:
        candidate = dict(base)
        candidate["units"] = dict(base["units"])
        candidate["units"].pop(key)
        with pytest.raises(ValidationError):
            ReviewLiveTenantBudget.model_validate_json(json.dumps(candidate))
    for patch in ({"amount": Decimal("20.01")}, {"requests": 101}, {"amount": Decimal("NaN")}):
        with pytest.raises(ValidationError):
            ReviewLiveTenantBudget.model_validate(reviewed_command().model_dump() | patch)
    for key, cap in (("messages", 21), ("duration_seconds", Decimal("901"))):
        values = reviewed_command().model_dump()
        values["units"][key] = cap
        with pytest.raises(ValidationError):
            ReviewLiveTenantBudget.model_validate(values)
