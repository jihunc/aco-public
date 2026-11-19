"""Persisted authority, stale jobs and canonical review foundations."""

from typing import Any

import pytest
from fastapi import HTTPException
from integration.test_outbox_worker import implementation
from sqlalchemy import select


@pytest.mark.parametrize("newer_state", ["succeeded", "executing", "queued"])
def test_delayed_claim_cannot_overwrite_newer_execution(
    monkeypatch: pytest.MonkeyPatch, newer_state: str
) -> None:
    import asyncio

    from action_support import SyntheticTransport, action_world, enqueue
    from pydantic import SecretStr
    from sqlalchemy.util import await_only

    from aco.core.settings import Settings
    from aco.features.actions.models import ScheduledAction
    from aco.features.actions.service import ActionService, worker_context
    from aco.worker.app import WorkerRuntime
    from aco.worker.jobs import execute_action

    with action_world() as world:
        ref = enqueue(world)
        runtime = WorkerRuntime(Settings(database_url=SecretStr(world.url)))
        executor = SyntheticTransport(world.url, crash="retry_once" if newer_state == "queued" else "")
        original = ActionService.claim_action

        async def run() -> None:
            entered, released = asyncio.Event(), asyncio.Event()
            count = 0

            def delayed(service: Any, ctx: Any, action_id: Any, expected_version: int) -> Any:
                nonlocal count
                count += 1
                if count == 1:
                    entered.set()
                    await_only(released.wait())
                return original(service, ctx, action_id, expected_version)

            monkeypatch.setattr(ActionService, "claim_action", delayed)
            try:
                async with runtime.app.open_async():
                    dispatched = await runtime.dispatcher.dispatch_outbox(10)
                    job = dispatched.job_ids[0]
                    stale = asyncio.create_task(
                        execute_action(runtime.sessions, ref.action_id, ref.version, executor, job_id=job)
                    )
                    await asyncio.wait_for(entered.wait(), 5)
                    if newer_state == "executing":
                        # A genuine separately committed claim, paused before its effect transaction.
                        async with runtime.sessions.begin() as db:
                            action = await db.get(ScheduledAction, ref.action_id)
                            assert action
                            await db.run_sync(
                                lambda store: original(
                                    ActionService(store), worker_context(action), ref.action_id, ref.version
                                )
                            )
                    else:
                        await execute_action(
                            runtime.sessions, ref.action_id, ref.version, executor, job_id=job
                        )
                    current = world.action(ref.action_id)
                    assert current.status == newer_state
                    before = (
                        current.version,
                        current.result,
                        current.last_error,
                        current.job_id,
                        current.lease_token,
                        current.due_at,
                    )
                    world.store.rollback()
                    released.set()
                    await stale
                    current = world.action(ref.action_id)
                    assert current.status == newer_state
                    assert (
                        current.version,
                        current.result,
                        current.last_error,
                        current.job_id,
                        current.lease_token,
                        current.due_at,
                    ) == before
            finally:
                released.set()
                executor.engine.dispose()
                await runtime.engine.dispose()

        asyncio.run(run())
        executions = world.executions(ref.action_id)
        assert len(executions) == 1 and executions[0].status == newer_state
        assert world.submission_count(ref.action_id) == (0 if newer_state == "executing" else 1)
        assert world.effect_count(ref.action_id) == (1 if newer_state == "succeeded" else 0)
        response = world.client.get(
            f"/api/workspaces/{world.ctx.tenant_id}/scheduled-actions/{ref.action_id}"
        )
        assert response.status_code == 200
        assert response.json()["status"] == response.json()["executions"][0]["status"] == newer_state


@pytest.mark.parametrize("change", ["membership", "tenant", "actor", "connection", "policy", "cancel"])
def test_current_authority_fences_queued_work(change: str) -> None:
    implementation()
    from action_support import action_world, enqueue, wait_action, workers

    from aco.features.actions.models import ScheduledAction
    from aco.features.actions.service import ActionService
    from aco.features.identity.models import Actor, Membership, ProviderConnection, Tenant
    from aco.features.policies.models import CollectionPolicy

    with action_world() as world:
        ref = enqueue(world)
        action = world.store.get(ScheduledAction, ref.action_id)
        assert action is not None
        if change == "membership":
            row = world.store.scalar(select(Membership).where(Membership.user_id == world.ctx.actor_id))
            assert row
            row.active = False
            row.version += 1
        elif change == "tenant":
            tenant = world.store.get(Tenant, world.ctx.tenant_id)
            assert tenant
            tenant.version += 1
        elif change == "actor":
            actor = world.store.get(Actor, action.worker_actor_id)
            assert actor
            actor.active = False
        elif change == "connection":
            connection = world.store.get(ProviderConnection, world.connection_id)
            assert connection
            connection.status = "BLOCKED"
        elif change == "policy":
            policy = world.store.scalar(
                select(CollectionPolicy).where(CollectionPolicy.tenant_id == world.ctx.tenant_id)
            )
            assert policy
            policy.version += 1
        else:
            ActionService(world.store).cancel_action(world.ctx, action.id, action.version)
        world.store.commit()
        with workers(world):
            wait_action(world, ref.action_id, "cancelled" if change == "cancel" else "failed")
        assert world.effect_count(ref.action_id) == 0


def test_source_revision_dedup_retains_original_events() -> None:
    implementation()
    from uuid import uuid4

    from action_support import action_world, enqueue, scheduled

    from aco.features.accounting.models import AccountingEvent
    from aco.features.actions.models import ActionSource
    from aco.features.actions.service import ActionService

    with action_world() as world:
        first = enqueue(world)
        second = ActionService(world.store).enqueue_action(world.ctx, scheduled(world))
        assert first.action_id == second.action_id
        source = world.store.get(AccountingEvent, world.source_event_id)
        assert source
        repeated_hint = AccountingEvent(
            tenant_id=source.tenant_id,
            provider_connection_id=source.provider_connection_id,
            external_event_id="second-hint-" + str(uuid4()),
            origin="SOURCE_HINT",
            invoice_id=source.invoice_id,
            source_revision=source.source_revision,
            envelope=source.envelope,
            provenance=source.provenance,
            result=source.result,
        )
        world.store.add(repeated_hint)
        world.store.flush()
        third = ActionService(world.store).enqueue_action(
            world.ctx, scheduled(world, source_event_id=repeated_hint.id)
        )
        world.store.commit()
        assert third.action_id == first.action_id
        assert set(
            world.store.scalars(
                select(ActionSource.source_event_id).where(ActionSource.action_id == first.action_id)
            )
        ) == {source.id, repeated_hint.id}
        conflicting = scheduled(world, payload={"unexpected": True})
        with pytest.raises((HTTPException, ValueError)):
            ActionService(world.store).enqueue_action(world.ctx, conflicting)


def test_internal_task_and_approval_are_persisted_non_grants() -> None:
    implementation()
    from action_support import action_world, enqueue

    from aco.features.review.internal_work import ApprovalProposal, InternalTaskCommand, InternalWorkService
    from aco.features.review.models import ApprovalDecision, ApprovalRequest, CollectionTask

    with action_world() as world:
        ref = enqueue(world)
        service = InternalWorkService(world.store)
        command = InternalTaskCommand(
            source_action_id=ref.action_id,
            title="Review source discrepancy",
            invoice_id=world.invoice_id,
            source_event_id=world.source_event_id,
        )
        task = service.apply_internal_task(world.ctx, command)
        assert service.apply_internal_task(world.ctx, command).task_id == task.task_id
        proposal = ApprovalProposal(
            action_id=ref.action_id,
            action_version=1,
            content="Synthetic proposal",
            destination="participant@example.test",
            channel="email",
            invoice_ids=(world.invoice_id,),
            evidence_ids=(world.source_event_id,),
            policy_version_id=world.policy_version_id,
            cost_bound="0",
            currency="AUD",
            consent_mode="no_capture",
        )
        approval = service.request_approval(world.ctx, proposal)
        world.store.commit()
        saved = world.store.get(CollectionTask, task.task_id)
        assert saved and saved.title == command.title and saved.deleted_at is None
        request = world.store.get(ApprovalRequest, approval.request_id)
        assert request and request.status == "pending" and request.initiator_id == world.ctx.actor_id
        assert world.store.scalar(select(ApprovalDecision)) is None
        assert world.submission_count(ref.action_id) == 0


def test_stale_lease_and_forged_library_job_cannot_execute_or_complete() -> None:
    import asyncio
    from uuid import uuid4

    from action_support import SyntheticTransport, action_world, enqueue
    from pydantic import SecretStr

    from aco.core.settings import Settings
    from aco.core.types import ActionResult, ActionStatus
    from aco.features.actions.service import ActionService, worker_context
    from aco.worker.app import WorkerRuntime
    from aco.worker.jobs import execute_action

    with action_world() as world:
        ref = enqueue(world)
        action = world.action(ref.action_id)
        runtime = WorkerRuntime(Settings(database_url=SecretStr(world.url)))

        async def run() -> None:
            async with runtime.app.open_async():
                result = await runtime.dispatcher.dispatch_outbox(10)
                await execute_action(
                    runtime.sessions,
                    ref.action_id,
                    ref.version,
                    SyntheticTransport(world.url),
                    job_id=result.job_ids[0] + 99,
                )
            await runtime.engine.dispose()

        world.store.rollback()
        asyncio.run(run())
        assert world.submission_count(ref.action_id) == 0
        action = world.action(ref.action_id)
        ctx = worker_context(action)
        lease = ActionService(world.store).claim_action(ctx, action.id, action.version)
        world.store.commit()
        with pytest.raises(HTTPException, match="STALE_EXECUTION_LEASE"):
            ActionService(world.store).complete_action(
                ctx,
                lease.model_copy(update={"token": uuid4()}),
                ActionResult(action_id=action.id, status=ActionStatus.SUCCEEDED),
            )
        world.store.rollback()


def test_expired_execution_lease_cannot_begin_effect() -> None:
    import asyncio
    from datetime import timedelta

    from action_support import action_world, enqueue
    from pydantic import SecretStr

    from aco.core.settings import Settings
    from aco.features.actions.service import ActionService, worker_context
    from aco.features.identity.models import now
    from aco.features.identity.service import IdentityService, Permission
    from aco.worker.app import WorkerRuntime

    with action_world() as world:
        ref = enqueue(world)
        runtime = WorkerRuntime(Settings(database_url=SecretStr(world.url)))

        async def dispatch() -> None:
            async with runtime.app.open_async():
                await runtime.dispatcher.dispatch_outbox(10)
            await runtime.engine.dispose()

        asyncio.run(dispatch())
        action = world.action(ref.action_id)
        ctx = worker_context(action)
        lease = ActionService(world.store).claim_action(ctx, action.id, action.version)
        action.lease_until = now() - timedelta(seconds=1)
        world.store.commit()
        with pytest.raises(HTTPException):
            IdentityService(world.store).authorize(
                ctx.model_copy(update={"lease_token": lease.token}), Permission.INVOICE_READ
            )


def test_missing_library_job_reference_cannot_invoke_executor() -> None:
    import asyncio

    from action_support import SyntheticTransport, action_world, enqueue
    from pydantic import SecretStr

    from aco.core.settings import Settings
    from aco.worker.app import WorkerRuntime
    from aco.worker.jobs import execute_action

    with action_world() as world:
        ref = enqueue(world)
        runtime = WorkerRuntime(Settings(database_url=SecretStr(world.url)))

        async def run() -> None:
            await execute_action(runtime.sessions, ref.action_id, ref.version, SyntheticTransport(world.url))
            await runtime.engine.dispose()

        asyncio.run(run())
        assert world.submission_count(ref.action_id) == 0
        assert world.action(ref.action_id).status == "queued"


def test_scheduled_internal_records_use_exact_action_grant() -> None:
    import asyncio

    from action_support import action_world, enqueue
    from pydantic import SecretStr

    from aco.core.settings import Settings
    from aco.features.review.internal_work import ApprovalProposal, InternalTaskCommand
    from aco.features.review.models import ApprovalRequest, CollectionTask
    from aco.worker.app import WorkerRuntime
    from aco.worker.jobs import DomainExecutor, execute_action

    with action_world() as world:
        source = enqueue(world)
        task_command = InternalTaskCommand(
            source_action_id=source.action_id,
            source_event_id=world.source_event_id,
            invoice_id=world.invoice_id,
            title="Inspect evidence",
        )
        task = enqueue(world, kind="internal_task", payload=task_command.model_dump(mode="json"))
        proposal = ApprovalProposal(
            action_id=source.action_id,
            action_version=source.version,
            content="Synthetic proposal",
            destination="participant@example.test",
            channel="email",
            invoice_ids=(world.invoice_id,),
            evidence_ids=(world.source_event_id,),
            policy_version_id=world.policy_version_id,
            cost_bound="0",
            currency="AUD",
            consent_mode="no_capture",
        )
        request = enqueue(world, kind="approval_request", payload=proposal.model_dump(mode="json"))
        runtime = WorkerRuntime(Settings(database_url=SecretStr(world.url)))

        async def run() -> None:
            async with runtime.app.open_async():
                await runtime.dispatcher.dispatch_outbox(10)
                for ref in (task, request):
                    action = world.action(ref.action_id)
                    await execute_action(
                        runtime.sessions, action.id, action.version, DomainExecutor(), job_id=action.job_id
                    )
            await runtime.engine.dispose()

        asyncio.run(run())
        assert world.action(task.action_id).status == "succeeded"
        assert world.action(request.action_id).status == "succeeded"
        saved = world.store.scalar(select(CollectionTask))
        approval = world.store.scalar(select(ApprovalRequest))
        assert saved and saved.creator_id == world.action(task.action_id).worker_actor_id
        assert approval and approval.status == "pending" and approval.granted_at is None
        assert approval.initiator_id == world.ctx.actor_id


def test_actor_migration_preserves_four_histories_and_empty_domain_reupgrade() -> None:
    from action_support import action_world
    from legacy_usage_support import legacy_snapshot
    from sqlalchemy import text

    from aco.features.usage.budgets import BudgetService
    from alembic import command

    with action_world() as world:
        budget = BudgetService(world.store)
        authority = budget.synthetic_authority(world.ctx, world.connection_id, "local")
        budget.create_scope(world.ctx, authority, "run")
        world.store.commit()
        # Exact pre-r4 column/history clone, not a lossy current-head downgrade.
        with legacy_snapshot(world.store, revision="r0003a_usage_invariants") as (legacy, config):
            tables = ("identity_audit_events", "accounting_observations", "usage_attempts", "budget_scopes")
            before = {
                table: list(legacy.scalars(text(f"SELECT row_to_json(t)::text FROM {table} t ORDER BY id")))
                for table in tables
            }
            assert all(before.values())
            legacy.rollback()
            command.upgrade(config, "r0004_actions")
            legacy.execute(
                text(
                    "INSERT INTO procrastinate_jobs(queue_name,task_name,args) "
                    "VALUES('aco','retained-history','{}')"
                )
            )
            legacy.commit()
            queue = list(
                legacy.scalars(text("SELECT row_to_json(t)::text FROM procrastinate_jobs t ORDER BY id"))
            )
            assert queue
            legacy.rollback()
            command.downgrade(config, "r0003a_usage_invariants")
            command.upgrade(config, "r0004_actions")
            assert (
                list(
                    legacy.scalars(text("SELECT row_to_json(t)::text FROM procrastinate_jobs t ORDER BY id"))
                )
                == queue
            )
            for table in tables:
                assert (
                    list(legacy.scalars(text(f"SELECT row_to_json(t)::text FROM {table} t ORDER BY id")))
                    == before[table]
                )
            assert (
                legacy.scalar(
                    text(
                        "SELECT count(*) FROM identity_users u LEFT JOIN identity_actors a ON a.id=u.id "
                        "WHERE a.kind IS DISTINCT FROM 'human'"
                    )
                )
                == 0
            )
            assert legacy.scalar(text("SELECT version_num FROM alembic_version")) == "r0004_actions"


def test_reservation_binding_rechecks_current_caps_without_service_settings() -> None:
    import json
    from decimal import Decimal
    from hashlib import sha256

    from action_support import action_world, enqueue, wait_action, workers

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
            budgets.set_limit(world.ctx, scope, "AUD", Decimal("1"), 10, synthetic=authority)
        from aco.features.accounting.models import InvoiceSnapshot

        invoice = world.store.get(InvoiceSnapshot, world.invoice_id)
        assert invoice
        key = sha256(
            json.dumps(
                [
                    str(world.ctx.tenant_id),
                    str(world.connection_id),
                    str(world.invoice_id),
                    invoice.source_revision,
                    "accounting_recheck",
                ],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        reservation = budgets.reserve_budget(
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
        world.store.commit()
        ref = enqueue(world, reservation_id=reservation.id)
        budgets.set_limit(world.ctx, scopes[0], "AUD", Decimal(".1"), 10, synthetic=authority)
        world.store.commit()
        with workers(world):
            wait_action(world, ref.action_id, "failed")
        assert world.submission_count(ref.action_id) == 0
        retained = world.store.get(BudgetReservation, reservation.id, populate_existing=True)
        assert retained and retained.status == "RESERVED" and retained.retained_amount == Decimal(".5")


def test_persisted_action_authority_is_immutable() -> None:
    from action_support import action_world, enqueue
    from sqlalchemy.exc import DBAPIError

    with action_world() as world:
        ref = enqueue(world)
        action = world.action(ref.action_id)
        action.command = {"forged": "grant"}
        with pytest.raises(DBAPIError, match="immutable action authority"):
            world.store.commit()
        world.store.rollback()


@pytest.mark.parametrize("damage", ["version", "catalog", "registration", "signature", "trigger"])
def test_migration_refuses_incompatible_or_partial_retained_library(damage: str) -> None:
    from action_support import action_world
    from legacy_usage_support import legacy_snapshot
    from sqlalchemy import text

    from alembic import command

    with action_world() as world:
        with legacy_snapshot(world.store, revision="r0003a_usage_invariants") as (legacy, config):
            command.upgrade(config, "r0004_actions")
            command.downgrade(config, "r0003a_usage_invariants")
            if damage == "version":
                legacy.execute(text("UPDATE aco_worker_schema SET version='unrecognized'"))
            elif damage == "catalog":
                legacy.execute(text("DROP FUNCTION procrastinate_update_heartbeat_v1(bigint)"))
            elif damage == "registration":
                legacy.execute(text("DROP FUNCTION procrastinate_register_worker_v1()"))
            elif damage == "signature":
                legacy.execute(text("DROP FUNCTION procrastinate_update_heartbeat_v1(bigint)"))
                legacy.execute(
                    text(
                        "CREATE FUNCTION procrastinate_update_heartbeat_v1(integer) RETURNS void "
                        "LANGUAGE SQL AS 'SELECT'"
                    )
                )
            else:
                legacy.execute(
                    text("DROP TRIGGER procrastinate_jobs_notify_queue_job_inserted_v1 ON procrastinate_jobs")
                )
            legacy.commit()
            # These exact retained-library definitions must refuse r4 re-entry,
            # not merely encounter a later, unrelated r33 downgrade guard.
            with pytest.raises(RuntimeError, match="retained worker schema"):
                command.upgrade(config, "r0004_actions")
            assert legacy.scalar(text("SELECT version_num FROM alembic_version")) == "r0003a_usage_invariants"
            assert legacy.scalar(text("SELECT to_regclass('procrastinate_jobs')")) is not None
            assert legacy.scalar(text("SELECT to_regclass('identity_actors')")) is None


@pytest.mark.parametrize("history", ["domain", "service"])
def test_service_history_prevents_lossy_downgrade_and_new_users_get_actor(history: str) -> None:
    from uuid import uuid4

    from action_support import action_world, enqueue
    from alembic.config import Config
    from legacy_usage_support import legacy_snapshot
    from sqlalchemy import MetaData, Table, text

    from aco.features.identity.models import Actor, AuditEvent, User
    from alembic import command

    with action_world() as world:
        user = User(
            issuer="synthetic-t07", subject=str(uuid4()), email="new@example.test", email_verified=True
        )
        world.store.add(user)
        world.store.commit()
        actor = world.store.get(Actor, user.id)
        assert actor and actor.kind == "human" and actor.user_id == user.id
        ref = enqueue(world)
        action = world.action(ref.action_id)
        worker = world.store.get(Actor, action.worker_actor_id)
        assert worker and worker.kind == "service" and worker.purpose == "durable_worker"
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", world.url.replace("%", "%%"))
        retained = world.store.scalar(
            text("SELECT row_to_json(t)::text FROM scheduled_actions t WHERE id=:id"), {"id": ref.action_id}
        )
        revision = world.store.scalar(text("SELECT version_num FROM alembic_version"))
        source_schema = world.store.scalar(text("SELECT current_schema()"))
        world.store.rollback()
        with pytest.raises(
            RuntimeError,
            match="Retained Voice grant, request and join authority requires non-destructive restore",
        ):
            command.downgrade(config, "r0003a_usage_invariants")
        assert world.store.scalar(text("SELECT version_num FROM alembic_version")) == revision
        assert (
            world.store.scalar(
                text("SELECT row_to_json(t)::text FROM scheduled_actions t WHERE id=:id"),
                {"id": ref.action_id},
            )
            == retained
        )
        world.store.rollback()
        with legacy_snapshot(world.store, revision="r0003a_usage_invariants") as (legacy, historical):
            command.upgrade(historical, "r0004_actions")
            # Typed historical service actor with the real enqueued action's
            # identity. No current service authority/provider call is minted.
            legacy.add(
                Actor(
                    id=worker.id,
                    kind=worker.kind,
                    user_id=None,
                    tenant_id=worker.tenant_id,
                    purpose=worker.purpose,
                    active=worker.active,
                )
            )
            legacy.flush()
            if history == "domain":
                # Copy only r4's actual columns from the genuinely validated
                # current action; its retained source/human IDs already exist.
                historical_table = Table("scheduled_actions", MetaData(), autoload_with=legacy.get_bind())
                columns = ",".join(f'"{column.name}"' for column in historical_table.columns)
                legacy.execute(
                    text(
                        f"INSERT INTO scheduled_actions ({columns}) "
                        f'SELECT {columns} FROM "{source_schema}".scheduled_actions WHERE id=:id'
                    ),
                    {"id": ref.action_id},
                )
                reason = "durable action/review history would be lost"
            else:
                # Genuine r4 FK-valid typed audit attribution; explicitly
                # synthetic fixture metadata, not a fabricated successful effect.
                legacy.add(
                    AuditEvent(
                        tenant_id=world.ctx.tenant_id,
                        actor_id=worker.id,
                        action="synthetic.historical_worker_review",
                        target_id=world.invoice_id,
                        correlation_id=world.ctx.correlation_id,
                        details={"synthetic_fixture": True, "provider_calls": "0"},
                    )
                )
                reason = "service-attributed history would be lost"
            legacy.commit()
            histories = {
                table: list(legacy.scalars(text(f"SELECT row_to_json(t)::text FROM {table} t ORDER BY id")))
                for table in ("identity_actors", "identity_audit_events", "scheduled_actions")
            }
            legacy.rollback()
            with pytest.raises(RuntimeError, match=reason):
                command.downgrade(historical, "r0003a_usage_invariants")
            assert legacy.scalar(text("SELECT version_num FROM alembic_version")) == "r0004_actions"
            for table, rows in histories.items():
                assert (
                    list(legacy.scalars(text(f"SELECT row_to_json(t)::text FROM {table} t ORDER BY id")))
                    == rows
                )


def test_service_context_cannot_cross_tenant_connection_or_mint_permissions() -> None:
    from uuid import uuid4

    from action_support import action_world, enqueue, scheduled

    from aco.features.actions.service import ActionService, worker_context
    from aco.features.identity.models import ProviderConnection, Tenant
    from aco.features.identity.service import IdentityService, Permission

    with action_world() as world:
        ref = enqueue(world)
        action = world.action(ref.action_id)
        context = worker_context(action)
        for forged in (
            context.model_copy(update={"tenant_id": uuid4()}),
            context.model_copy(update={"actor_id": world.ctx.actor_id}),
            context.model_copy(update={"action_id": uuid4()}),
        ):
            with pytest.raises(HTTPException):
                IdentityService(world.store).authorize(forged, Permission.INVOICE_READ)
            world.store.rollback()
        with pytest.raises(HTTPException):
            IdentityService(world.store).authorize(context, Permission.SETTINGS)
        world.store.rollback()
        foreign = Tenant(name="Other synthetic workspace", timezone="UTC")
        world.store.add(foreign)
        world.store.flush()
        connection = ProviderConnection(
            tenant_id=foreign.id, provider="demo", provider_type="accounting", status="READY"
        )
        world.store.add(connection)
        world.store.flush()
        with pytest.raises(HTTPException, match="INVALID_ACTION_SCOPE"):
            ActionService(world.store).enqueue_action(
                world.ctx, scheduled(world, provider_connection_id=connection.id)
            )
        world.store.rollback()
        assert world.client.get(
            f"/api/workspaces/{foreign.id}/scheduled-actions/{ref.action_id}"
        ).status_code in {403, 404}
        assert world.submission_count(ref.action_id) == 0


def test_accounting_worker_scope_is_enforced_at_service_and_port_boundaries() -> None:
    import asyncio

    from action_support import action_world, enqueue
    from pydantic import SecretStr
    from sqlalchemy import func

    from aco.core.settings import Settings
    from aco.features.accounting.ingest import AccountingService
    from aco.features.actions.service import ActionService, worker_context
    from aco.features.identity.models import ProviderConnection
    from aco.features.usage.models import UsageAttempt
    from aco.integrations.demo_accounting.adapter import DemoAccountingAdapter, DemoAccountingSource
    from aco.integrations.ports.accounting import AccountingFailure
    from aco.worker.app import WorkerRuntime

    with action_world() as world:
        ref = enqueue(world)
        second = ProviderConnection(
            tenant_id=world.ctx.tenant_id,
            provider="demo",
            provider_type="accounting",
            status="READY",
            external_account_id="other-synthetic-org",
        )
        world.store.add(second)
        world.store.commit()
        runtime = WorkerRuntime(Settings(database_url=SecretStr(world.url)))

        async def run() -> None:
            async with runtime.app.open_async():
                await runtime.dispatcher.dispatch_outbox(10)
            action = world.action(ref.action_id)
            context = worker_context(action)
            lease = ActionService(world.store).claim_action(context, action.id, action.version)
            world.store.commit()
            context = context.model_copy(update={"lease_token": lease.token})
            async with runtime.sessions.begin() as db:
                adapter = DemoAccountingAdapter(db, world.connection_id)
                before = await db.scalar(select(func.count()).select_from(UsageAttempt))
                for call in (
                    lambda: AccountingService(db, adapter).sync_accounting(context, None),
                    lambda: AccountingService(db, adapter).customers(context),
                    lambda: adapter.get_invoice(context, "INV-102"),
                    lambda: DemoAccountingSource(db, world.connection_id).get_invoice(context, "INV-102"),
                    lambda: DemoAccountingSource(db, second.id).connection(context),
                    lambda: DemoAccountingAdapter(db, second.id).get_invoice(context, "INV-101"),
                ):
                    with pytest.raises((HTTPException, AccountingFailure)):
                        await call()
                    assert await db.scalar(select(func.count()).select_from(UsageAttempt)) == before
            await runtime.engine.dispose()

        asyncio.run(run())


@pytest.mark.parametrize("kind", ["internal_task", "approval_request"])
def test_queued_service_grant_cannot_write_before_due_claim_or_job_mapping(kind: str) -> None:
    from datetime import timedelta

    from action_support import action_world, enqueue

    from aco.features.actions.service import worker_context
    from aco.features.identity.models import now
    from aco.features.review.internal_work import ApprovalProposal, InternalTaskCommand, InternalWorkService
    from aco.features.review.models import ApprovalRequest, CollectionTask

    with action_world() as world:
        source = enqueue(world)
        command = (
            InternalTaskCommand(
                source_action_id=source.action_id,
                source_event_id=world.source_event_id,
                invoice_id=world.invoice_id,
                title="Must await durable claim",
            )
            if kind == "internal_task"
            else ApprovalProposal(
                action_id=source.action_id,
                action_version=source.version,
                content="Must await durable claim",
                destination="participant@example.test",
                channel="email",
                invoice_ids=(world.invoice_id,),
                evidence_ids=(world.source_event_id,),
                policy_version_id=world.policy_version_id,
                cost_bound="0",
                currency="AUD",
                consent_mode="no_capture",
            )
        )
        queued = enqueue(
            world, kind=kind, payload=command.model_dump(mode="json"), due_at=now() + timedelta(hours=1)
        )
        ctx = worker_context(world.action(queued.action_id))
        with pytest.raises(HTTPException):
            if isinstance(command, InternalTaskCommand):
                InternalWorkService(world.store).apply_internal_task(ctx, command)
            else:
                InternalWorkService(world.store).request_approval(ctx, command)
        assert world.store.scalar(select(CollectionTask)) is None
        assert world.store.scalar(select(ApprovalRequest)) is None


def test_due_action_cannot_be_claimed_without_persisted_library_mapping() -> None:
    from action_support import action_world, enqueue

    from aco.features.actions.service import ActionService, worker_context

    with action_world() as world:
        ref = enqueue(world)
        action = world.action(ref.action_id)
        with pytest.raises(HTTPException):
            ActionService(world.store).claim_action(worker_context(action), action.id, action.version)
        assert world.action(ref.action_id).attempts == 0


def test_actual_library_payload_is_rechecked_at_claim_and_effect() -> None:
    import asyncio
    import json
    from uuid import uuid4

    from action_support import action_world, enqueue
    from pydantic import SecretStr
    from sqlalchemy import text

    from aco.core.settings import Settings
    from aco.features.actions.service import ActionService, worker_context
    from aco.features.identity.service import IdentityService, Permission
    from aco.worker.app import WorkerRuntime

    with action_world() as world:
        ref = enqueue(world)
        runtime = WorkerRuntime(Settings(database_url=SecretStr(world.url)))

        async def dispatch() -> None:
            async with runtime.app.open_async():
                await runtime.dispatcher.dispatch_outbox(10)
            await runtime.engine.dispose()

        asyncio.run(dispatch())
        action = world.action(ref.action_id)
        ctx = worker_context(action)
        world.store.execute(
            text("UPDATE procrastinate_jobs SET args=CAST(:args AS jsonb) WHERE id=:id"),
            {"id": action.job_id, "args": json.dumps({"action_id": str(uuid4()), "expected_version": 1})},
        )
        world.store.commit()
        with pytest.raises(HTTPException, match="INVALID_ACTION_JOB_BINDING"):
            ActionService(world.store).claim_action(ctx, action.id, action.version)
        assert action.attempts == 0
        world.store.execute(
            text("UPDATE procrastinate_jobs SET args=CAST(:args AS jsonb) WHERE id=:id"),
            {"id": action.job_id, "args": json.dumps({"action_id": str(action.id), "expected_version": 1})},
        )
        lease = ActionService(world.store).claim_action(ctx, action.id, action.version)
        world.store.commit()
        world.store.execute(
            text("UPDATE procrastinate_jobs SET task_name='forged.task' WHERE id=:id"), {"id": action.job_id}
        )
        world.store.commit()
        with pytest.raises(HTTPException, match="INVALID_ACTION_JOB_BINDING"):
            IdentityService(world.store).authorize(
                ctx.model_copy(update={"lease_token": lease.token}), Permission.DEMO_PAYMENT
            )
        assert world.submission_count(action.id) == 0


@pytest.mark.parametrize(
    "surface", ["receivables", "usage", "demo_payment", "cancel", "reconcile", "enqueue"]
)
def test_human_only_surfaces_reject_valid_action_context(surface: str) -> None:
    import asyncio
    from typing import cast

    from action_support import action_world, enqueue, scheduled
    from pydantic import SecretStr

    from aco.core.context import TenantContext
    from aco.core.settings import Settings
    from aco.features.accounting.seed import demo_guard
    from aco.features.actions.service import ActionService, worker_context
    from aco.features.identity.service import Permission
    from aco.features.receivables.queries import ReceivableFilter, ReceivableQueries
    from aco.features.review.internal_work import InternalTaskCommand
    from aco.features.usage.service import UsageService
    from aco.worker.app import WorkerRuntime

    with action_world() as world:
        source = enqueue(world)
        if surface == "cancel":
            command = InternalTaskCommand(
                source_action_id=source.action_id,
                source_event_id=world.source_event_id,
                invoice_id=world.invoice_id,
                title="Exact task only",
            )
            grant = enqueue(world, kind="internal_task", payload=command.model_dump(mode="json"))
        else:
            grant = source
        runtime = WorkerRuntime(Settings(database_url=SecretStr(world.url)))

        async def run() -> None:
            async with runtime.app.open_async():
                await runtime.dispatcher.dispatch_outbox(10)
            action = world.action(grant.action_id)
            ctx = worker_context(action)
            lease = ActionService(world.store).claim_action(ctx, action.id, action.version)
            world.store.commit()
            # Deliberately bypass the annotation to challenge the runtime boundary.
            human = cast(TenantContext, ctx.model_copy(update={"lease_token": lease.token}))
            async with runtime.sessions.begin() as db:
                reason = "HUMAN_INITIATION_REQUIRED" if surface == "enqueue" else "HUMAN_CONTEXT_REQUIRED"
                with pytest.raises(HTTPException, match=reason):
                    if surface == "receivables":
                        await ReceivableQueries(db).list_receivables(human, ReceivableFilter())
                    elif surface == "usage":
                        await db.run_sync(lambda store: UsageService(store).list_usage(human))
                    elif surface == "demo_payment":
                        await demo_guard(db, human, "local", Permission.DEMO_PAYMENT)
                    elif surface == "cancel":
                        await db.run_sync(
                            lambda store: ActionService(store).cancel_action(
                                human, source.action_id, source.version
                            )
                        )
                    elif surface == "enqueue":
                        await db.run_sync(
                            lambda store: ActionService(store).enqueue_action(human, scheduled(world))
                        )
                    else:
                        await db.run_sync(
                            lambda store: ActionService(store).reconcile_unknown(human, source.action_id)
                        )
            await runtime.engine.dispose()

        asyncio.run(run())
        assert world.submission_count(grant.action_id) == 0
