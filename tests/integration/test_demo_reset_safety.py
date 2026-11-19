"""Actual synthetic reset fences; retained provider uncertainty is never cleared."""

import asyncio
import importlib.util
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from action_support import World, action_world, enqueue, run_attempts
from boundary_evidence import BoundaryObserver
from fastapi import FastAPI, HTTPException
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from aco.core.context import ServiceContext
from aco.core.settings import Settings
from aco.core.types import ActionResult, ActionStatus
from aco.features.accounting.models import AccountingEvent, DemoReset, InvoiceSnapshot
from aco.features.actions.service import ActionLease, ActionService, worker_context
from aco.features.identity.models import Actor, AuditEvent, Tenant
from aco.features.identity.service import IdentityService, Permission
from aco.worker.app import WorkerRuntime

CASES = [
    case
    for case in json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]
    if case["domain"] == "demo"
]


def implementation() -> None:
    assert importlib.util.find_spec("aco.features.accounting.demo_controls") is not None, (
        "Generation-fenced demo reset service is not implemented"
    )


def another_action(world: World, index: int) -> UUID:
    invoices = list(
        world.store.scalars(
            select(InvoiceSnapshot)
            .where(InvoiceSnapshot.tenant_id == world.ctx.tenant_id)
            .order_by(InvoiceSnapshot.number)
        )
    )
    invoice = invoices[index]
    event = world.store.scalar(
        select(AccountingEvent).where(
            AccountingEvent.invoice_id == invoice.id,
            AccountingEvent.source_revision == invoice.source_revision,
        )
    )
    assert event is not None
    key = enqueue(world, invoice_id=invoice.id, source_event_id=event.id).action_id
    assert isinstance(key, UUID)
    return key


def claim(world: World, action_id: UUID) -> tuple[ServiceContext, ActionLease]:
    async def publish() -> None:
        runtime = WorkerRuntime(Settings(database_url=SecretStr(world.url)))
        try:
            async with runtime.app.open_async():
                await runtime.dispatcher.dispatch_outbox(100)
        finally:
            await runtime.engine.dispose()

    world.store.rollback()
    asyncio.run(publish())
    action = world.action(action_id)
    context = worker_context(action)
    lease = ActionService(world.store).claim_action(context, action.id, action.version)
    context = worker_context(action, lease.token)
    world.store.commit()
    return context, lease


def test_reset_fences_running_workers_and_preserves_system_audit() -> None:
    implementation()
    from aco.features.accounting.demo_controls import DemoResetCommand, reset_demo_workspace

    with action_world() as world:
        uncertain = enqueue(world)
        run_attempts(world, uncertain, "UNKNOWN_SIDE_EFFECT", attempts=1)
        unknown_before = world.store.scalar(
            text("SELECT to_jsonb(a) FROM scheduled_actions a WHERE id=:id"), {"id": uncertain.action_id}
        )
        world.store.rollback()
        pending_id = another_action(world, 1)
        pending_context = worker_context(world.action(pending_id))
        IdentityService(world.store).authorize(pending_context, Permission.INVOICE_READ)
        world.store.rollback()
        executing_id = another_action(world, 2)
        old_context, old_lease = claim(world, executing_id)
        tenant = world.store.get(Tenant, world.ctx.tenant_id)
        assert tenant is not None
        generation = tenant.version
        actors_before = {
            actor.id: actor.version
            for actor in world.store.scalars(select(Actor).where(Actor.tenant_id == tenant.id))
        }
        world.store.rollback()
        command = DemoResetCommand(
            request_id=uuid4(),
            expected_generation=generation,
            evidence="Owner requests synthetic demo restart",
        )
        result = reset_demo_workspace(world.store, world.ctx, command, app_env="local")
        world.store.commit()
        assert result.previous_generation == generation
        assert result.generation == generation + 1
        assert result.outbound_paused and not result.automation_enabled
        assert result.cancelled_action_ids == (pending_id,)
        assert result.unknown_action_ids == (executing_id,)
        assert result.preserved_uncertain_action_ids == (uncertain.action_id,)
        assert world.action(pending_id).status == "cancelled"
        assert world.action(executing_id).status == "unknown_outcome"
        assert (
            world.store.scalar(
                text("SELECT to_jsonb(a) FROM scheduled_actions a WHERE id=:id"), {"id": uncertain.action_id}
            )
            == unknown_before
        )
        for actor_id, version in actors_before.items():
            actor = world.store.get(Actor, actor_id, populate_existing=True)
            assert actor is not None and actor.version == version + 1
        world.store.rollback()
        with pytest.raises(HTTPException):
            IdentityService(world.store).authorize(pending_context, Permission.INVOICE_READ)
        world.store.rollback()
        with pytest.raises(HTTPException, match="STALE_EXECUTION_LEASE"):
            ActionService(world.store).complete_action(
                old_context,
                old_lease,
                ActionResult(action_id=executing_id, status=ActionStatus.SUCCEEDED),
            )
        world.store.rollback()
        replay = reset_demo_workspace(world.store, world.ctx, command, app_env="local")
        world.store.commit()
        assert replay == result
        record = world.store.scalar(select(DemoReset).where(DemoReset.request_id == command.request_id))
        assert record is not None
        for sql in (
            "UPDATE demo_resets SET generation=generation+1 WHERE id=:id",
            "DELETE FROM demo_resets WHERE id=:id",
        ):
            with pytest.raises(DBAPIError, match="history is immutable"), world.store.begin_nested():
                world.store.execute(text(sql), {"id": record.id})
        assert (
            world.store.scalar(
                select(AuditEvent.id).where(
                    AuditEvent.tenant_id == world.ctx.tenant_id,
                    AuditEvent.action == "demo.reset",
                    AuditEvent.target_id == command.request_id,
                )
            )
            is not None
        )
        assert world.submission_count(pending_id) == 0
        assert world.submission_count(executing_id) == 0
        assert world.submission_count(uncertain.action_id) == 1
        world.store.rollback()
        stale = command.model_copy(update={"request_id": uuid4()})
        with pytest.raises(HTTPException, match="DEMO_GENERATION_CONFLICT"):
            reset_demo_workspace(world.store, world.ctx, stale, app_env="local")
        world.store.rollback()
        changed = command.model_copy(update={"evidence": "Changed captured restart evidence"})
        with pytest.raises(HTTPException, match="DEMO_RESET_REQUEST_CONFLICT"):
            reset_demo_workspace(world.store, world.ctx, changed, app_env="local")


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_boundary_demo_guards_and_generation(
    case: dict[str, object], boundary_observer: BoundaryObserver
) -> None:
    implementation()
    from accounting_support import seed
    from identity_support import actor_client

    from aco.features.accounting.demo_controls import DemoResetCommand, reset_demo_workspace

    expected = case["expected"]
    if case["id"] == "demo_env_without_demo_tenant_denied":
        with actor_client("owner") as (client, store, _user, tenant_id):
            seed(client, tenant_id)
            tenant = store.get(Tenant, tenant_id)
            assert tenant is not None
            tenant.is_demo = False
            store.commit()
            invoice = store.scalar(select(InvoiceSnapshot).where(InvoiceSnapshot.tenant_id == tenant_id))
            assert invoice is not None
            before = store.execute(text("SELECT to_jsonb(i) FROM invoice_snapshots i ORDER BY id")).all()
            response = client.post(
                f"/api/workspaces/{tenant_id}/demo/accounting/simulate-payment",
                json={
                    "provider_connection_id": str(invoice.provider_connection_id),
                    "operation_id": str(uuid4()),
                    "external_invoice_id": invoice.external_invoice_id,
                    "amount": {"minor_units": 1, "currency": invoice.currency, "exponent": invoice.exponent},
                },
            )
            store.rollback()
            after = store.execute(text("SELECT to_jsonb(i) FROM invoice_snapshots i ORDER BY id")).all()
            actual = {
                "allowed": response.status_code < 400,
                "real_ledger_writes": int(before != after),
            }
            assert actual == expected
            boundary_observer.assert_observed(str(case["id"]), actual)
    elif case["id"] == "demo_reset_fences_stale_generation":
        with action_world() as world:
            tenant = world.store.get(Tenant, world.ctx.tenant_id)
            assert tenant is not None
            tenant.version = 3
            world.store.commit()
            uncertain = enqueue(world)
            run_attempts(world, uncertain, "UNKNOWN_SIDE_EFFECT", attempts=1)
            prior = world.store.scalar(
                text("SELECT to_jsonb(a) FROM scheduled_actions a WHERE id=:id"), {"id": uncertain.action_id}
            )
            world.store.rollback()
            stale_id = another_action(world, 1)
            old_context, lease = claim(world, stale_id)
            result = reset_demo_workspace(
                world.store,
                world.ctx,
                DemoResetCommand(
                    request_id=uuid4(), expected_generation=3, evidence="Fixture owner reviewed reset"
                ),
                app_env="local",
            )
            world.store.commit()
            assert result.generation == 4
            allowed = True
            try:
                IdentityService(world.store).authorize(old_context, Permission.DEMO_PAYMENT)
            except HTTPException:
                allowed = False
            world.store.rollback()
            late_allowed = True
            try:
                ActionService(world.store).complete_action(
                    old_context, lease, ActionResult(action_id=stale_id, status=ActionStatus.SUCCEEDED)
                )
            except HTTPException:
                late_allowed = False
            world.store.rollback()
            actual = {
                "stale_worker_mutation_allowed": late_allowed,
                "stale_provider_submission_allowed": allowed,
                "unknown_audit_preserved": world.store.scalar(
                    text("SELECT to_jsonb(a) FROM scheduled_actions a WHERE id=:id"),
                    {"id": uncertain.action_id},
                )
                == prior,
            }
            assert actual == expected
            boundary_observer.assert_observed(str(case["id"]), actual)
    else:
        pytest.fail("Unconsumed demo boundary case")


def test_reset_releases_only_exact_attached_proven_unused_reservation() -> None:
    from decimal import Decimal
    from hashlib import sha256

    from aco.features.accounting.demo_controls import DemoResetCommand, reset_demo_workspace
    from aco.features.usage.budgets import BudgetService
    from aco.features.usage.dto import UsageReservationRequest
    from aco.features.usage.models import BudgetReservation

    with action_world() as world:
        budgets = BudgetService(world.store)
        authority = budgets.synthetic_authority(world.ctx, world.connection_id, "local")
        scopes = tuple(
            budgets.create_scope(world.ctx, authority, dimension).id
            for dimension in ("tenant", "provider", "run", "call", "demo_session")
        )
        for scope in scopes:
            budgets.set_limit(world.ctx, scope, "AUD", Decimal("10"), 100, synthetic=authority)
        invoice = world.store.get(InvoiceSnapshot, world.invoice_id)
        assert invoice is not None
        operation = sha256(
            json.dumps(
                [
                    str(world.ctx.tenant_id),
                    str(world.connection_id),
                    str(invoice.id),
                    invoice.source_revision,
                    "accounting_recheck",
                ],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        reservations = [
            budgets.reserve_budget(
                world.ctx,
                UsageReservationRequest(
                    operation_key=key,
                    provider_connection_id=world.connection_id,
                    scope_ids=scopes,
                    currency="AUD",
                    amount=Decimal(".5"),
                ),
                authority=authority,
            )
            for key in (operation, "unattached-no-proof")
        ]
        unused_id, retained_id = (row.id for row in reservations)
        world.store.commit()
        enqueue(world, reservation_id=unused_id)
        tenant = world.store.get(Tenant, world.ctx.tenant_id)
        assert tenant is not None
        result = reset_demo_workspace(
            world.store,
            world.ctx,
            DemoResetCommand(
                request_id=uuid4(),
                expected_generation=tenant.version,
                evidence="Owner reviewed exact unused reservation",
            ),
            app_env="local",
        )
        world.store.commit()
        assert result.released_reservation_ids == (unused_id,)
        unused = world.store.get(BudgetReservation, unused_id, populate_existing=True)
        retained = world.store.get(BudgetReservation, retained_id, populate_existing=True)
        assert unused is not None and retained is not None
        assert (unused.status, unused.released_amount, unused.retained_amount) == (
            "NO_EFFECT",
            Decimal(".5"),
            Decimal(0),
        )
        assert (retained.status, retained.retained_amount) == ("RESERVED", Decimal(".5"))


def test_reset_http_current_owner_csrf_environment_and_exact_replay() -> None:
    from concurrent.futures import ThreadPoolExecutor

    from accounting_support import seed
    from identity_support import actor_client

    from aco.features.accounting.demo_api import router
    from aco.features.identity.models import Membership

    with actor_client("owner") as (client, store, user, tenant_id):
        app = client.app
        assert isinstance(app, FastAPI)
        app.include_router(router)
        seed(client, tenant_id)
        tenant = store.get(Tenant, tenant_id)
        assert tenant is not None
        command = {
            "request_id": str(uuid4()),
            "expected_generation": tenant.version,
            "evidence": "Owner reviewed the retained synthetic history",
        }
        store.rollback()
        url = f"/api/workspaces/{tenant_id}/demo/reset"
        csrf = client.headers.pop("x-csrf-token")
        assert client.post(url, json=command).status_code == 403
        client.headers["x-csrf-token"] = csrf
        for role in ("finance_manager", "operator", "viewer"):
            member = store.scalar(
                select(Membership).where(Membership.tenant_id == tenant_id, Membership.user_id == user.id)
            )
            assert member is not None
            member.role = role
            store.commit()
            assert client.post(url, json=command).status_code == 403
        assert member is not None
        member = store.get(Membership, member.id, populate_existing=True)
        assert member is not None
        member.role = "owner"
        store.commit()
        prior_settings = app.state.settings
        app.state.settings = prior_settings.model_copy(update={"app_env": "test"})
        assert client.post(url, json=command).status_code == 403
        app.state.settings = prior_settings
        response = client.post(url, json=command)
        assert response.status_code == 200, response.text
        assert client.post(url, json=command).json() == response.json()
        store.rollback()
        assert (
            store.scalar(
                select(AuditEvent.id).where(
                    AuditEvent.tenant_id == tenant_id, AuditEvent.action == "demo.reset"
                )
            )
            is not None
        )

        generation = response.json()["generation"]
        store.rollback()
        with ThreadPoolExecutor(max_workers=2) as pool:
            requests = [
                pool.submit(
                    client.post,
                    url,
                    json={**command, "request_id": str(uuid4()), "expected_generation": generation},
                )
                for _ in range(2)
            ]
            statuses = sorted(future.result().status_code for future in requests)
        assert statuses == [200, 409]
        store.rollback()
        current = store.get(Tenant, tenant_id, populate_existing=True)
        assert current is not None and current.version == generation + 1
