"""Every reliability fixture executes production transitions and inspects stored outcomes."""

import json
from pathlib import Path
from typing import Any

import pytest
from boundary_evidence import BoundaryObserver
from integration.test_outbox_worker import implementation

CASES = [
    c
    for c in json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]
    if c["domain"] == "reliability"
]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_boundary_safe_retry_and_unknown(case: dict[str, Any], boundary_observer: BoundaryObserver) -> None:
    implementation()
    from action_support import action_world, enqueue, run_attempts

    from aco.features.actions.service import ActionService

    with action_world() as world:
        ref = enqueue(world)
        if case["id"] == "retry_unknown_after_vendor_key_expiry":
            run_attempts(world, ref, case["input"]["outcome"], attempts=case["input"]["attempts"])
            before = world.submission_count(ref.action_id)
            key = world.action(ref.action_id).operation_key
            world.expire_vendor_key(ref.action_id, case["input"]["vendor_key_expired"])
            ActionService(world.store).reconcile_unknown(world.ctx, ref.action_id)
            world.store.commit()
            action = world.action(ref.action_id)
            observed = {
                "automatic_submissions": world.submission_count(ref.action_id) - before,
                "status": action.status,
                "internal_operation_key_retained": action.operation_key == key,
            }
        elif case["id"] == "retry_verified_no_effect_bounded":
            run_attempts(
                world,
                ref,
                case["input"]["error"],
                may_have_side_effect=case["input"]["may_have_side_effect"],
                retry_after=case["input"]["provider_retry_after_seconds"],
            )
            action = world.action(ref.action_id)
            executions = world.executions(ref.action_id)
            observed = {
                "total_attempts_max": world.submission_count(ref.action_id),
                "retry_delays_seconds": [
                    e.retry_delay_seconds for e in executions if e.retry_delay_seconds is not None
                ],
                "on_exhaustion": action.status,
            }
        else:
            pytest.fail("Unconsumed reliability fixture")
        assert observed == case["expected"]
        boundary_observer.assert_observed(case["id"], observed)


def test_untrusted_provider_result_remains_unknown_without_resend() -> None:
    import asyncio

    from action_support import SyntheticTransport, action_world, enqueue
    from pydantic import SecretStr

    from aco.core.settings import Settings
    from aco.core.types import ProviderReference
    from aco.worker.app import WorkerRuntime
    from aco.worker.jobs import execute_action

    class WrongProvider(SyntheticTransport):
        async def execute(self, ctx: Any, action: Any, db: Any) -> Any:
            result = await super().execute(ctx, action, db)
            return result.model_copy(
                update={
                    "provider_ref": ProviderReference(
                        provider="foreign-vendor",
                        connection_id=action.provider_connection_id,
                        external_id="untrusted",
                    )
                }
            )

    with action_world() as world:
        ref = enqueue(world)
        runtime = WorkerRuntime(Settings(database_url=SecretStr(world.url)))

        async def run() -> None:
            async with runtime.app.open_async():
                result = await runtime.dispatcher.dispatch_outbox(10)
                await execute_action(
                    runtime.sessions,
                    ref.action_id,
                    ref.version,
                    WrongProvider(world.url),
                    job_id=result.job_ids[0],
                )
            await runtime.engine.dispose()

        asyncio.run(run())

        assert world.effect_count(ref.action_id) == 1
        assert world.action(ref.action_id).status == "unknown_outcome"
        assert world.action(ref.action_id).automatic_resend is False


@pytest.mark.parametrize("refetch_first", [False, True], ids=["after-retry", "after-refetch"])
def test_queued_recovery_fences_stale_claim_and_acknowledgement(refetch_first: bool) -> None:
    import asyncio
    from decimal import Decimal
    from hashlib import sha256

    from action_support import SyntheticTransport, action_world, enqueue
    from fastapi import HTTPException
    from procrastinate.jobs import Status
    from pydantic import SecretStr
    from sqlalchemy import select, text

    from aco.core.settings import Settings
    from aco.features.accounting.models import InvoiceSnapshot
    from aco.features.actions.models import OutboxEvent
    from aco.features.actions.service import ActionService, worker_context
    from aco.features.identity.models import AuditEvent
    from aco.features.usage.budgets import BudgetService
    from aco.features.usage.dto import UsageReservationRequest
    from aco.worker.app import WorkerRuntime
    from aco.worker.jobs import execute_action
    from aco.worker.recovery import recover_stalled

    with action_world() as world:
        budgets = BudgetService(world.store)
        authority = budgets.synthetic_authority(world.ctx, world.connection_id, "local")
        scopes = tuple(
            budgets.create_scope(world.ctx, authority, dimension).id
            for dimension in ("tenant", "provider", "run", "call", "demo_session")
        )
        for scope in scopes:
            budgets.set_limit(world.ctx, scope, "AUD", Decimal("1"), 10, synthetic=authority)
        invoice = world.store.get(InvoiceSnapshot, world.invoice_id)
        assert invoice
        operation_key = sha256(
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
                operation_key=operation_key,
                provider_connection_id=world.connection_id,
                scope_ids=scopes,
                currency="AUD",
                amount=Decimal(".5"),
            ),
            authority=authority,
        )
        world.store.commit()
        ref = enqueue(world, reservation_id=reservation.id)
        budget_before = world.store.scalar(
            text("SELECT to_jsonb(r) FROM budget_reservations r WHERE id=:id"), {"id": reservation.id}
        )
        world.store.rollback()
        runtime = WorkerRuntime(Settings(database_url=SecretStr(world.url)))

        async def run() -> None:
            entered, released = asyncio.Event(), asyncio.Event()
            task: asyncio.Task[None] | None = None

            class PausedTransport(SyntheticTransport):
                async def execute(self, ctx: Any, action: Any, db: Any) -> Any:
                    entered.set()
                    await released.wait()
                    return await super().execute(ctx, action, db)

            manager = runtime.app.job_manager
            try:
                async with runtime.app.open_async():
                    await runtime.dispatcher.dispatch_outbox(10)
                    before = world.action(ref.action_id)
                    old_ctx = worker_context(before)
                    stable = (
                        before.operation_key,
                        before.due_at,
                        before.command,
                        before.source_revision,
                        before.policy_version_id,
                        before.initiator_id,
                        before.reservation_id,
                    )
                    world.store.rollback()
                    old_worker = await manager.register_worker()
                    old = await manager.fetch_job(["aco"], old_worker)
                    assert old
                    await asyncio.sleep(3.2)
                    assert await recover_stalled(runtime.app, runtime.sessions) == 1
                    replacement_worker = await manager.register_worker()
                    historical = None
                    if refetch_first:
                        historical = await manager.fetch_job(["aco"], replacement_worker)
                        assert historical and historical.id == old.id
                    with pytest.raises(HTTPException) as denied:
                        async with runtime.sessions.begin() as db:
                            await db.run_sync(
                                lambda store: ActionService(store).claim_action(
                                    old_ctx, ref.action_id, ref.version
                                )
                            )
                    assert denied.value.status_code == 403
                    action = world.action(ref.action_id)
                    assert (action.status, action.version, action.attempts, action.job_id) == (
                        "queued",
                        2,
                        0,
                        None,
                    )
                    assert world.executions(ref.action_id) == []
                    assert (
                        world.store.scalar(
                            text("SELECT to_jsonb(r) FROM budget_reservations r WHERE id=:id"),
                            {"id": reservation.id},
                        )
                        == budget_before
                    )
                    audits = list(
                        world.store.scalars(
                            select(AuditEvent).where(
                                AuditEvent.action == "action.recovered_unclaimed",
                                AuditEvent.tenant_id == world.ctx.tenant_id,
                            )
                        )
                    )
                    assert len(audits) == 1 and audits[0].actor_id == action.worker_actor_id
                    assert (
                        action.operation_key,
                        action.due_at,
                        action.command,
                        action.source_revision,
                        action.policy_version_id,
                        action.initiator_id,
                        action.reservation_id,
                    ) == stable
                    world.store.rollback()
                    assert (await runtime.dispatcher.dispatch_outbox(10)).dispatched == 1
                    if historical is None:
                        historical = await manager.fetch_job(["aco"], replacement_worker)
                        assert historical and historical.id == old.id
                    current = await manager.fetch_job(["aco"], replacement_worker)
                    assert current and current.id != old.id
                    task = asyncio.create_task(
                        execute_action(
                            runtime.sessions, ref.action_id, 2, PausedTransport(world.url), job_id=current.id
                        )
                    )
                    await asyncio.wait_for(entered.wait(), 5)
                    assert world.action(ref.action_id).status == "executing"
                    world.store.rollback()
                    # Actual stale original acknowledgement is keyed only by old JobRef.
                    # It must never touch the distinct job carrying the current execution.
                    await manager.finish_job(old, Status.SUCCEEDED, False)
                    state = world.store.scalar(
                        text("SELECT status::text FROM procrastinate_jobs WHERE id=:id"), {"id": current.id}
                    )
                    assert state == "doing"
                    assert world.submission_count(ref.action_id) == 0
                    released.set()
                    await task
                    await manager.finish_job(current, Status.SUCCEEDED, False)
                    await manager.unregister_worker(old_worker)
                    await manager.unregister_worker(replacement_worker)
                    assert await recover_stalled(runtime.app, runtime.sessions) == 0
                    final = world.action(ref.action_id)
                    assert (final.status, final.version, final.attempts, final.job_id) == (
                        "succeeded",
                        2,
                        1,
                        current.id,
                    )
                    assert final.operation_key == stable[0]
                    executions = world.executions(ref.action_id)
                    assert len(executions) == 1 and executions[0].status == "succeeded"
                    outbox = list(
                        world.store.scalars(
                            select(OutboxEvent)
                            .where(OutboxEvent.action_id == final.id)
                            .order_by(OutboxEvent.action_version)
                        )
                    )
                    assert [(row.action_version, row.job_id) for row in outbox] == [
                        (1, old.id),
                        (2, current.id),
                    ]
                    assert world.submission_count(ref.action_id) == world.effect_count(ref.action_id) == 1
            finally:
                released.set()
                if task and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await runtime.engine.dispose()

        asyncio.run(run())


def test_backoff_and_finished_time_start_after_provider_response() -> None:
    import asyncio
    from datetime import timedelta

    from action_support import SyntheticTransport, action_world, enqueue
    from pydantic import SecretStr

    from aco.core.settings import Settings
    from aco.features.identity.models import now
    from aco.worker.app import WorkerRuntime
    from aco.worker.jobs import execute_action

    with action_world() as world:
        ref = enqueue(world)
        start = now()
        response = start + timedelta(seconds=90)
        ticks = iter((start, response))
        runtime = WorkerRuntime(Settings(database_url=SecretStr(world.url)))

        async def run() -> None:
            async with runtime.app.open_async():
                result = await runtime.dispatcher.dispatch_outbox(10)
                await execute_action(
                    runtime.sessions,
                    ref.action_id,
                    ref.version,
                    SyntheticTransport(world.url, "TEMPORARILY_UNAVAILABLE", retry_after=120),
                    job_id=result.job_ids[0],
                    clock=lambda: next(ticks),
                )
            await runtime.engine.dispose()

        asyncio.run(run())
        assert world.action(ref.action_id).due_at == response + timedelta(seconds=120)
        assert world.executions(ref.action_id)[0].finished_at == response


@pytest.mark.parametrize("uncertain", [False, True], ids=["confirmed-no-effect", "uncertain-no-resend"])
def test_actual_accounting_failure_preserves_retry_classification(
    monkeypatch: pytest.MonkeyPatch, uncertain: bool
) -> None:
    import asyncio
    from datetime import timedelta

    from action_support import action_world, enqueue
    from pydantic import SecretStr
    from sqlalchemy import select

    from aco.core.settings import Settings
    from aco.core.types import ProviderErrorCode, ProviderFailure
    from aco.features.usage.models import UsageAttempt
    from aco.integrations.demo_accounting.adapter import DemoAccountingSource
    from aco.integrations.ports.accounting import AccountingFailure
    from aco.worker.app import WorkerRuntime
    from aco.worker.jobs import DomainExecutor, execute_action

    with action_world() as world:
        ref = enqueue(world)
        key = world.action(ref.action_id).operation_key
        runtime = WorkerRuntime(Settings(database_url=SecretStr(world.url)))
        calls = []

        async def unavailable(self: Any, ctx: Any, invoice_key: str) -> Any:
            calls.append(invoice_key)
            raise AccountingFailure(
                ProviderFailure(
                    code=ProviderErrorCode.TEMPORARILY_UNAVAILABLE,
                    retryable=not uncertain,
                    may_have_side_effect=uncertain,
                    retry_after=timedelta(seconds=125),
                    request_id="source-unavailable",
                )
            )

        monkeypatch.setattr(DemoAccountingSource, "get_invoice", unavailable)

        async def run() -> None:
            try:
                async with runtime.app.open_async():
                    for attempt in range(1, (1 if uncertain else 3) + 1):
                        dispatched = await runtime.dispatcher.dispatch_outbox(10)
                        action = world.action(ref.action_id)
                        version, due = action.version, action.due_at
                        response = due + timedelta(seconds=90)
                        ticks = iter((due, response))
                        await execute_action(
                            runtime.sessions,
                            ref.action_id,
                            version,
                            DomainExecutor(),
                            job_id=dispatched.job_ids[0],
                            clock=ticks.__next__,
                        )
                        current = world.action(ref.action_id)
                        execution = world.executions(ref.action_id)[-1]
                        assert execution.error == {
                            "code": "TEMPORARILY_UNAVAILABLE",
                            "retryable": not uncertain,
                            "may_have_side_effect": uncertain,
                            "request_id": "source-unavailable",
                            "retry_after": "PT2M5S",
                        }
                        assert execution.finished_at == response
                        if not uncertain and attempt < 3:
                            delay = 125 if attempt == 1 else 300
                            assert current.status == "queued"
                            assert execution.retry_delay_seconds == delay
                            assert current.due_at == response + timedelta(seconds=delay)
                        else:
                            assert current.status == ("unknown_outcome" if uncertain else "dead_letter")
                            assert execution.retry_delay_seconds is None
                        # A duplicate of this library invocation never calls the port again.
                        await execute_action(
                            runtime.sessions,
                            ref.action_id,
                            version,
                            DomainExecutor(),
                            job_id=dispatched.job_ids[0],
                        )
            finally:
                await runtime.engine.dispose()

        asyncio.run(run())
        final = world.action(ref.action_id)
        assert final.operation_key == key and final.automatic_resend is False
        count = 1 if uncertain else 3
        assert final.attempts == count and calls == ["INV-101"] * count
        usage = list(
            world.store.scalars(select(UsageAttempt).where(UsageAttempt.actor_id == final.worker_actor_id))
        )
        assert len(usage) == count
        assert all(
            (a.operation, a.outcome, a.business_outcome, a.error_reference)
            == ("get_invoice", "TEMPORARILY_UNAVAILABLE", "ROLLED_BACK", "source-unavailable")
            for a in usage
        )


def test_delayed_stalled_snapshot_cannot_fence_a_reclaimed_job(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from action_support import action_world, enqueue
    from pydantic import SecretStr

    from aco.core.settings import Settings
    from aco.features.actions.models import ScheduledAction
    from aco.features.actions.service import ActionService, worker_context
    from aco.worker.app import WorkerRuntime
    from aco.worker.recovery import recover_stalled

    with action_world() as world:
        ref = enqueue(world)
        runtime = WorkerRuntime(Settings(database_url=SecretStr(world.url)))

        async def run() -> None:
            entered, released = asyncio.Event(), asyncio.Event()
            manager = runtime.app.job_manager
            original = manager.get_stalled_jobs
            calls = 0

            async def delayed(**kwargs: Any) -> Any:
                nonlocal calls
                jobs = list(await original(**kwargs))
                calls += 1
                if calls == 1:
                    assert len(jobs) == 1
                    entered.set()
                    await released.wait()
                return jobs

            try:
                async with runtime.app.open_async():
                    await runtime.dispatcher.dispatch_outbox(10)
                    old_worker = await manager.register_worker()
                    old = await manager.fetch_job(["aco"], old_worker)
                    assert old
                    # Real maintained unregister leaves this unclaimed domain job stalled.
                    await manager.unregister_worker(old_worker)
                    monkeypatch.setattr(manager, "get_stalled_jobs", delayed)
                    stale = asyncio.create_task(recover_stalled(runtime.app, runtime.sessions))
                    await asyncio.wait_for(entered.wait(), 5)
                    # A maintained manual retry creates a changed incarnation
                    # without production queued-generation rotation. A stale
                    # production inspection still cannot fence its fresh lease.
                    await manager.retry_job(old)
                    active_worker = await manager.register_worker()
                    try:
                        reclaimed = await manager.fetch_job(["aco"], active_worker)
                        assert reclaimed and reclaimed.id == old.id and reclaimed.attempts == 1
                        async with runtime.sessions.begin() as db:
                            action = await db.get(ScheduledAction, ref.action_id)
                            assert action
                            await db.run_sync(
                                lambda store: ActionService(store).claim_action(
                                    worker_context(action), ref.action_id, ref.version
                                )
                            )
                        assert world.action(ref.action_id).status == "executing"
                        released.set()
                        assert await stale == 0
                        assert world.action(ref.action_id).status == "executing"
                        assert world.executions(ref.action_id)[0].status == "executing"
                    finally:
                        await manager.unregister_worker(active_worker)
            finally:
                released.set()
                await runtime.engine.dispose()

        asyncio.run(run())
        assert world.submission_count(ref.action_id) == world.effect_count(ref.action_id) == 0


def test_claim_between_recovery_transactions_cannot_be_orphaned(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from action_support import action_world, enqueue
    from fastapi import HTTPException
    from procrastinate.jobs import Status
    from pydantic import SecretStr
    from sqlalchemy import text
    from sqlalchemy.util import await_only

    from aco.core.settings import Settings
    from aco.features.actions.service import ActionService, worker_context
    from aco.worker.app import WorkerRuntime
    from aco.worker.jobs import DomainExecutor, execute_action
    from aco.worker.recovery import recover_stalled

    with action_world() as world:
        ref = enqueue(world)
        key = world.action(ref.action_id).operation_key
        runtime = WorkerRuntime(Settings(database_url=SecretStr(world.url)))
        original = ActionService.locked

        async def run() -> None:
            entered, released = asyncio.Event(), asyncio.Event()
            recovery_task: asyncio.Task[int] | None = None
            lock_count = 0

            def paused(service: Any, ctx: Any, action_id: Any) -> Any:
                nonlocal lock_count
                if asyncio.current_task() is recovery_task:
                    lock_count += 1
                    if lock_count == 2:
                        entered.set()
                        await_only(released.wait())
                return original(service, ctx, action_id)

            monkeypatch.setattr(ActionService, "locked", paused)
            manager = runtime.app.job_manager
            try:
                async with runtime.app.open_async():
                    await runtime.dispatcher.dispatch_outbox(10)
                    old_ctx = worker_context(world.action(ref.action_id))
                    world.store.rollback()
                    old_worker = await manager.register_worker()
                    old = await manager.fetch_job(["aco"], old_worker)
                    assert old
                    await asyncio.sleep(3.2)  # Actual missing heartbeat, unchanged library incarnation.
                    recovery_task = asyncio.create_task(recover_stalled(runtime.app, runtime.sessions))
                    await asyncio.wait_for(entered.wait(), 5)
                    with pytest.raises(HTTPException) as denied:
                        async with runtime.sessions.begin() as db:
                            await db.run_sync(
                                lambda store: ActionService(store).claim_action(
                                    old_ctx, ref.action_id, ref.version
                                )
                            )
                    assert denied.value.status_code == 403
                    assert world.action(ref.action_id).status == "queued"
                    world.store.rollback()
                    released.set()
                    first = await recovery_task
                    await manager.unregister_worker(old_worker)
                    replacement_worker = await manager.register_worker()
                    replacement = await manager.fetch_job(["aco"], replacement_worker)
                    assert replacement and replacement.id == old.id
                    await execute_action(
                        runtime.sessions, ref.action_id, ref.version, DomainExecutor(), job_id=replacement.id
                    )
                    await manager.finish_job(replacement, Status.SUCCEEDED, False)
                    assert (await runtime.dispatcher.dispatch_outbox(10)).dispatched == 1
                    current = await manager.fetch_job(["aco"], replacement_worker)
                    assert current and current.id != old.id
                    await execute_action(
                        runtime.sessions, ref.action_id, 2, DomainExecutor(), job_id=current.id
                    )
                    await manager.finish_job(current, Status.SUCCEEDED, False)
                    await manager.unregister_worker(replacement_worker)
                    assert await recover_stalled(runtime.app, runtime.sessions) == 0
                    final = world.action(ref.action_id)
                    job = world.store.execute(
                        text("SELECT status::text,attempts FROM procrastinate_jobs WHERE id=:id"),
                        {"id": old.id},
                    ).one()
                    print(
                        {
                            "gap_action": str(final.id),
                            "domain": final.status,
                            "job": tuple(job),
                            "first_recovered": first,
                            "library_workers": [old_worker, replacement_worker],
                        },
                        flush=True,
                    )
                    assert final.status == "succeeded", "Current generation must execute exactly once"
                    assert first == 1
                    assert (final.version, final.job_id, final.attempts) == (2, current.id, 1)
                    assert final.operation_key == key and final.automatic_resend is False
                    executions = world.executions(ref.action_id)
                    assert len(executions) == 1
                    assert executions[0].lease_token == final.lease_token
                    assert executions[0].status == "succeeded" and executions[0].finished_at
                    assert tuple(job) == ("succeeded", 2)
                    assert world.submission_count(ref.action_id) == world.effect_count(ref.action_id) == 0
            finally:
                released.set()
                if recovery_task and not recovery_task.done():
                    recovery_task.cancel()
                    await asyncio.gather(recovery_task, return_exceptions=True)
                await runtime.engine.dispose()

        asyncio.run(run())
