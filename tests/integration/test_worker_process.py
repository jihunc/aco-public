"""Process lifetime, persisted library heartbeat and atomic enqueue mapping."""

import asyncio
import multiprocessing
import time
from multiprocessing.process import BaseProcess
from typing import Any
from uuid import UUID

import pytest
from action_support import World, action_world, enqueue, wait_action, workers
from sqlalchemy import text


def api_child(url: str, started_at: float) -> None:
    def phase(name: str) -> None:
        print({"api_child_phase": name, "elapsed_seconds": time.monotonic() - started_at}, flush=True)

    phase("entered")
    import uvicorn
    from pydantic import SecretStr

    from aco.api.app import create_app
    from aco.core.settings import Settings

    phase("imports_ready")
    app = create_app(Settings(database_url=SecretStr(url)))
    phase("app_created")
    try:
        uvicorn.run(app, host="127.0.0.1", port=58000, log_level="error")
    finally:
        phase("server_returned")


def worker_failure_state(world: World, action_id: UUID, processes: list[BaseProcess]) -> dict[str, object]:
    """Bounded diagnostics from this fixture schema; never dump payloads or connection config."""
    world.store.rollback()
    snapshot: dict[str, object] = {
        "processes": [
            {"pid": process.pid, "alive": process.is_alive(), "exitcode": process.exitcode}
            for process in processes
        ]
    }
    queries = {
        "action": "SELECT id, status, version, job_id, attempts, due_at "
        "FROM scheduled_actions WHERE id=:action_id",
        "outbox": "SELECT action_version, job_id, due_at, dispatched_at FROM action_outbox "
        "WHERE action_id=:action_id ORDER BY action_version DESC LIMIT 10",
        "jobs": "SELECT id, task_name, status::text, scheduled_at, attempts, worker_id "
        "FROM procrastinate_jobs WHERE id IN "
        "(SELECT job_id FROM action_outbox WHERE action_id=:action_id) "
        "OR task_name='aco.maintenance' ORDER BY id DESC LIMIT 10",
        "workers": "SELECT id, last_heartbeat, "
        "EXTRACT(EPOCH FROM clock_timestamp()-last_heartbeat) AS heartbeat_age_seconds "
        "FROM procrastinate_workers ORDER BY id DESC LIMIT 10",
        "waits": "SELECT DISTINCT a.pid, a.state, a.wait_event_type, a.wait_event, "
        "l.granted, l.mode FROM pg_locks l JOIN pg_class c ON c.oid=l.relation "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "JOIN pg_stat_activity a ON a.pid=l.pid "
        "WHERE n.nspname=current_schema() AND a.pid<>pg_backend_pid() "
        "ORDER BY a.pid, l.mode LIMIT 20",
    }
    try:
        world.store.execute(text("SET LOCAL statement_timeout='1s'"))
        for name, statement in queries.items():
            snapshot[name] = [
                dict(row._mapping) for row in world.store.execute(text(statement), {"action_id": action_id})
            ]
    except Exception as error:
        # Diagnostics must not hide the original completion assertion or disclose SQL/URLs.
        snapshot["diagnostic_error_type"] = type(error).__name__
    finally:
        world.store.rollback()
    return snapshot


def test_worker_failure_state_preserves_queued_action_and_excludes_private_payload() -> None:
    with action_world() as world:
        source = enqueue(world)
        ref = enqueue(
            world,
            kind="internal_task",
            payload={
                "source_action_id": str(source.action_id),
                "source_event_id": str(world.source_event_id),
                "invoice_id": str(world.invoice_id),
                "title": "Synthetic diagnostic scope",
                "notes": "not-for-process-diagnostics",
            },
        )
        before = world.action(ref.action_id)
        expected = (before.status, before.version, before.job_id, before.attempts)
        snapshot = worker_failure_state(world, ref.action_id, [])
        assert "diagnostic_error_type" not in snapshot
        assert set(snapshot) == {"processes", "action", "outbox", "jobs", "workers", "waits"}
        assert snapshot["processes"] == []
        assert snapshot["workers"] == []
        assert snapshot["jobs"] == []
        assert "queued" in str(snapshot["action"])
        assert "not-for-process-diagnostics" not in str(snapshot)
        after = world.action(ref.action_id)
        assert (after.status, after.version, after.job_id, after.attempts) == expected
        assert world.effect_count(ref.action_id) == 0


def test_worker_health_tracks_real_process_death_and_api_independence() -> None:
    with action_world() as world:
        assert world.client.get("/health/ready").json()["checks"]["worker"] == "UNAVAILABLE"
        with workers(world):
            deadline = time.monotonic() + 15
            while world.client.get("/health/ready").json()["checks"]["worker"] != "READY":
                assert time.monotonic() < deadline
                time.sleep(0.05)
            ref = enqueue(world)
            # TestClient closes while separate worker keeps its own DB/queue lifetime.
            world.client.close()
            wait_action(world, ref.action_id, "succeeded")
        from fastapi.testclient import TestClient

        with TestClient(world.client.app) as reopened:
            deadline = time.monotonic() + 8
            while reopened.get("/health/ready").json()["checks"]["worker"] == "READY":
                assert time.monotonic() < deadline
                time.sleep(0.05)
            assert reopened.get("/health/live").status_code == 200


def test_actual_api_process_can_die_while_worker_finishes_due_work() -> None:
    from datetime import timedelta

    import httpx

    from aco.features.identity.models import now

    with action_world() as world:
        started_at = time.monotonic()
        api = multiprocessing.get_context("spawn").Process(target=api_child, args=(world.url, started_at))
        api.start()
        try:
            with httpx.Client(trust_env=False) as browser:
                deadline = time.monotonic() + 15
                while True:
                    try:
                        assert browser.get("http://127.0.0.1:58000/health/live").status_code == 200
                        break
                    except httpx.TransportError:
                        assert time.monotonic() < deadline, {
                            "phase": "waiting_for_api",
                            "elapsed_seconds": time.monotonic() - started_at,
                            "child_alive": api.is_alive(),
                            "child_exitcode": api.exitcode,
                        }
                        time.sleep(0.05)
                with workers(world) as processes:
                    ref = enqueue(world, due_at=now() + timedelta(seconds=3))
                    api.kill()
                    api.join(5)
                    browser.close()
                    try:
                        wait_action(world, ref.action_id, "succeeded")
                    except AssertionError:
                        print(
                            {"worker_failure_state": worker_failure_state(world, ref.action_id, processes)},
                            flush=True,
                        )
                        raise
                    assert world.effect_count(ref.action_id) == 1
        finally:
            print(
                {
                    "phase": "api_cleanup",
                    "elapsed_seconds": time.monotonic() - started_at,
                    "child_alive": api.is_alive(),
                    "child_exitcode": api.exitcode,
                },
                flush=True,
            )
            if api.is_alive():
                api.kill()
            api.join(5)


def test_library_enqueue_and_mapping_share_transaction_rollback() -> None:
    from pydantic import SecretStr
    from sqlalchemy import event

    from aco.core.settings import Settings
    from aco.worker.app import WorkerRuntime

    with action_world() as world:
        ref = enqueue(world)
        runtime = WorkerRuntime(Settings(database_url=SecretStr(world.url)))

        async def run() -> None:
            def fail_commit(connection: Any) -> None:
                raise RuntimeError("Synthetic dispatch final-commit failure")

            async with runtime.app.open_async():
                event.listen(runtime.engine.sync_engine, "commit", fail_commit)
                try:
                    import pytest

                    with pytest.raises(RuntimeError, match="final-commit"):
                        await runtime.dispatcher.dispatch_outbox(10)
                finally:
                    event.remove(runtime.engine.sync_engine, "commit", fail_commit)
                world.store.rollback()
                assert world.store.scalar(text("SELECT count(*) FROM procrastinate_jobs")) == 0
                assert world.action(ref.action_id).job_id is None
                result = await runtime.dispatcher.dispatch_outbox(10)
                assert result.dispatched == 1
                assert world.action(ref.action_id).job_id == result.job_ids[0]
            await runtime.engine.dispose()

        asyncio.run(run())


def test_maintained_worker_obeys_real_sixty_second_retry_delay() -> None:
    with action_world() as world:
        ref = enqueue(world)
        with workers(world, crash="retry_once"):
            deadline = time.monotonic() + 20
            while world.action(ref.action_id).attempts == 0:
                assert time.monotonic() < deadline
                time.sleep(0.05)
            deadline = time.monotonic() + 80
            while world.action(ref.action_id).status != "succeeded":
                assert time.monotonic() < deadline
                time.sleep(0.1)
        action = world.action(ref.action_id)
        assert action.attempts == world.submission_count(ref.action_id) == 2
        assert world.effect_count(ref.action_id) == 1
        executions = world.executions(ref.action_id)
        assert executions[0].retry_delay_seconds == 60
        times = list(
            world.store.scalars(
                text("SELECT at FROM synthetic_submissions WHERE action_id=:id ORDER BY at"),
                {"id": ref.action_id},
            )
        )
        assert (times[1] - times[0]).total_seconds() >= 60


def test_default_worker_records_genuine_service_usage_and_source_observation() -> None:
    from sqlalchemy import select

    from aco.features.accounting.models import AccountingObservation
    from aco.features.identity.models import AuditEvent
    from aco.features.usage.models import UsageAttempt

    with action_world() as world:
        ref = enqueue(world)
        with workers(world, crash="domain"):
            wait_action(world, ref.action_id, "succeeded")
        actor = world.action(ref.action_id).worker_actor_id
        assert actor != world.ctx.actor_id
        assert world.store.scalar(select(UsageAttempt).where(UsageAttempt.actor_id == actor)) is not None
        assert (
            world.store.scalar(select(AccountingObservation).where(AccountingObservation.actor_id == actor))
            is not None
        )
        assert world.store.scalar(select(AuditEvent).where(AuditEvent.actor_id == actor)) is not None


def split_gap_worker_child(url: str, role: str, ready: Any, claim_gate: Any, committed: Any) -> None:
    """Timing-only faults around genuine claim/commit and automatic maintenance."""
    import os

    from action_support import SyntheticTransport
    from fastapi import HTTPException
    from pydantic import SecretStr
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    from aco.core.settings import Settings
    from aco.features.actions.service import ActionService
    from aco.worker.app import WorkerRuntime

    patcher = pytest.MonkeyPatch()
    if role == "claim":
        original_claim = ActionService.claim_action

        def delayed_claim(service: Any, *args: Any, **kwargs: Any) -> Any:
            ready.set()
            # Deliberately suspend the whole original process, including heartbeat,
            # until maintenance has committed its first QUEUED inspection.
            assert claim_gate.wait(20)
            try:
                result = original_claim(service, *args, **kwargs)
            except HTTPException as error:
                assert error.status_code == 403
                committed.set()  # The old invocation was fenced before any claim.
                os._exit(81)
            service.db.info["crash_after_gap_claim"] = True
            return result

        def crash_after_commit(session: Session) -> None:
            if session.info.get("crash_after_gap_claim"):
                committed.set()
                os._exit(80)

        patcher.setattr(ActionService, "claim_action", delayed_claim)
        event.listen(Session, "after_commit", crash_after_commit)
    else:
        original_locked = ActionService.locked
        calls = 0

        def pause_second_lock(service: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 2:
                claim_gate.set()
                assert committed.wait(10)
            return original_locked(service, *args, **kwargs)

        patcher.setattr(ActionService, "locked", pause_second_lock)
    asyncio.run(WorkerRuntime(Settings(database_url=SecretStr(url)), executor=SyntheticTransport(url)).run())


def test_automatic_recovery_fences_delayed_unclaimed_process() -> None:
    from sqlalchemy import select

    from aco.features.actions.models import OutboxEvent

    with action_world() as world:
        ref = enqueue(world)
        key = world.action(ref.action_id).operation_key
        context = multiprocessing.get_context("spawn")
        ready, claim_gate, committed = context.Event(), context.Event(), context.Event()
        original = context.Process(
            target=split_gap_worker_child, args=(world.url, "claim", ready, claim_gate, committed)
        )
        recovery = context.Process(
            target=split_gap_worker_child, args=(world.url, "recover", ready, claim_gate, committed)
        )
        original.start()
        try:
            assert ready.wait(15)
            before = world.action(ref.action_id)
            assert before.status == "queued" and before.job_id is not None
            job_id = before.job_id
            world.store.rollback()
            recovery.start()
            print({"split_gap_original_pid": original.pid, "maintenance_pid": recovery.pid}, flush=True)
            original.join(25)
            assert original.exitcode == 81 and committed.is_set()
            # Production periodic maintenance, never a test call to recover/retry,
            # must converge after fencing the original invocation's stale generation.
            deadline = time.monotonic() + 20
            while True:
                world.store.rollback()
                job = world.store.execute(
                    text("SELECT status::text,attempts FROM procrastinate_jobs WHERE id=:id"),
                    {"id": job_id},
                ).one()
                count = world.store.scalar(
                    text(
                        "SELECT count(*) FROM procrastinate_jobs "
                        "WHERE task_name='aco.maintenance' AND status='succeeded'"
                    )
                )
                if job.status == "succeeded" and count >= 4:
                    break
                assert time.monotonic() < deadline
                time.sleep(0.1)
            final = wait_action(world, ref.action_id, "succeeded")
            print(
                {
                    "split_gap_final": final.status,
                    "job": tuple(job),
                    "submissions": world.submission_count(ref.action_id),
                    "effects": world.effect_count(ref.action_id),
                },
                flush=True,
            )
            assert final.status == "succeeded"
            assert final.version == 2 and final.job_id != job_id and final.attempts == 1
            assert final.operation_key == key and final.automatic_resend is False
            assert tuple(job) == ("succeeded", 2)
            executions = world.executions(ref.action_id)
            assert len(executions) == 1
            assert executions[0].status == "succeeded" and executions[0].finished_at
            assert executions[0].lease_token == final.lease_token
            outbox = list(world.store.scalars(select(OutboxEvent).where(OutboxEvent.action_id == final.id)))
            assert sorted((row.action_version, row.job_id) for row in outbox) == [
                (1, job_id),
                (2, final.job_id),
            ]
            assert world.submission_count(ref.action_id) == world.effect_count(ref.action_id) == 1
        finally:
            claim_gate.set()
            for child in (original, recovery):
                if child.pid is not None:
                    if child.is_alive():
                        child.kill()
                    child.join(5)


def test_crash_before_due_work_recovers_once_and_during_attempt_stays_unknown() -> None:
    from datetime import timedelta

    from aco.features.identity.models import now

    with action_world() as world:
        ref = enqueue(world, due_at=now() + timedelta(seconds=3))
        with workers(world) as first:
            first[0].kill()
            first[0].join(5)
        with workers(world):
            wait_action(world, ref.action_id, "succeeded")
        assert world.submission_count(ref.action_id) == world.effect_count(ref.action_id) == 1
    with action_world() as world:
        ref = enqueue(world)
        with workers(world, crash="before_acceptance") as first:
            first[0].join(20)
            assert first[0].exitcode == 72
        assert world.action(ref.action_id).status == "executing"
        with workers(world):
            wait_action(world, ref.action_id, "unknown_outcome")
            time.sleep(1)
        assert world.submission_count(ref.action_id) == world.effect_count(ref.action_id) == 0
        assert world.action(ref.action_id).attempts == 1


def crash_after_retry_commit_child(url: str) -> None:
    import os

    from action_support import SyntheticTransport
    from pydantic import SecretStr

    from aco.core.settings import Settings
    from aco.worker.app import WorkerRuntime

    runtime = WorkerRuntime(
        Settings(database_url=SecretStr(url)), executor=SyntheticTransport(url, crash="retry_once")
    )
    original = runtime.app.job_manager.finish_job

    async def intercept(job: Any, status: Any, delete_job: bool) -> None:
        if job.task_name == "aco.execute":
            # Domain handler has returned/committed; maintained acknowledgement has not run.
            os._exit(79)
        await original(job, status, delete_job)

    runtime.app.job_manager.finish_job = intercept  # type: ignore[method-assign]
    asyncio.run(runtime.run())


def test_retry_commit_crash_recovers_owned_historical_job_without_resubmission() -> None:
    from sqlalchemy import select

    from aco.features.actions.models import OutboxEvent

    with action_world() as world:
        ref = enqueue(world)
        child = multiprocessing.get_context("spawn").Process(
            target=crash_after_retry_commit_child, args=(world.url,)
        )
        child.start()
        print({"post_commit_crash_pid": child.pid}, flush=True)
        try:
            child.join(25)
            assert child.exitcode == 79
        finally:
            if child.is_alive():
                child.kill()
                child.join(5)
        action = world.action(ref.action_id)
        assert (action.status, action.version, action.attempts) == ("queued", 2, 1)
        old = world.store.execute(
            text("SELECT id,status::text FROM procrastinate_jobs WHERE task_name='aco.execute' ORDER BY id")
        ).one()
        assert old.status == "doing"
        due, key = action.due_at, action.operation_key
        with workers(world, count=2):
            deadline = time.monotonic() + 15
            while True:
                world.store.rollback()
                count = world.store.scalar(
                    text(
                        "SELECT count(*) FROM procrastinate_jobs "
                        "WHERE task_name='aco.maintenance' AND status='succeeded'"
                    )
                )
                historical_status = world.store.scalar(
                    text("SELECT status::text FROM procrastinate_jobs WHERE id=:id"), {"id": old.id}
                )
                if count >= 5 and historical_status == "succeeded":
                    break
                assert time.monotonic() < deadline, (count, historical_status)
                time.sleep(0.1)
            world.store.rollback()
            states = list(
                world.store.execute(
                    text(
                        "SELECT id,status::text,args FROM procrastinate_jobs "
                        "WHERE task_name='aco.execute' ORDER BY id"
                    )
                ).mappings()
            )
            assert next(row for row in states if row["id"] == old.id)["status"] == "succeeded"
            action = world.action(ref.action_id)
            assert (action.status, action.version, action.attempts) == ("queued", 2, 1)
            assert action.due_at == due and action.operation_key == key
            assert action.job_id != old.id
            assert next(row for row in states if row["id"] == action.job_id)["status"] == "todo"
        assert world.submission_count(ref.action_id) == 1 and world.effect_count(ref.action_id) == 0
        assert len(world.executions(ref.action_id)) == 1
        outbox = list(
            world.store.scalars(
                select(OutboxEvent)
                .where(OutboxEvent.action_id == ref.action_id)
                .order_by(OutboxEvent.action_version)
            )
        )
        assert [(row.action_version, row.job_id) for row in outbox] == [(1, old.id), (2, action.job_id)]


@pytest.mark.parametrize(
    "independent_import", [False, True], ids=["own-reconciliation", "stale-independent-import"]
)
def test_default_worker_reconciles_new_source_but_rejects_independently_changed_projection(
    independent_import: bool,
) -> None:
    from copy import deepcopy
    from datetime import date

    from sqlalchemy import select

    from aco.features.accounting.ingest import AccountingService
    from aco.features.accounting.models import (
        AccountingEvent,
        AccountingObservation,
        DemoSourceRecord,
        InvoiceSnapshot,
        InvoiceSourceRevision,
        VerifiedPaymentAllocation,
    )
    from aco.features.identity.models import now
    from aco.features.usage.models import UsageAttempt
    from aco.integrations.demo_accounting.adapter import DemoAccountingAdapter

    with action_world() as world:
        ref = enqueue(world)
        source = world.store.scalar(
            select(DemoSourceRecord).where(
                DemoSourceRecord.tenant_id == world.ctx.tenant_id,
                DemoSourceRecord.provider_connection_id == world.connection_id,
                DemoSourceRecord.kind == "invoice",
                DemoSourceRecord.external_id == "INV-101",
            )
        )
        assert source
        original_payload = deepcopy(source.payload)
        payload = deepcopy(source.payload)
        payload.update(
            due_date="2026-12-30",
            status="partially_paid",
            source_status="PARTIALLY_PAID",
            outstanding={"minor_units": 9000, "currency": "AUD", "exponent": 2},
        )
        common = dict(
            tenant_id=source.tenant_id,
            provider_connection_id=source.provider_connection_id,
            organisation_id=source.organisation_id,
            effective_at=now(),
            observed_at=now(),
        )
        world.store.add(
            DemoSourceRecord(**common, kind="invoice", external_id="INV-101", revision=2, payload=payload)
        )
        world.store.add(
            DemoSourceRecord(
                **common,
                kind="payment",
                external_id="recheck-cash/1",
                revision=1,
                payload={
                    "external_payment_id": "recheck-cash",
                    "external_allocation_id": "1",
                    "external_invoice_id": "INV-101",
                    "kind": "cash",
                    "amount": {"minor_units": 1000, "currency": "AUD", "exponent": 2},
                    "parent_payment_id": None,
                    "parent_allocation_id": None,
                    "verified": True,
                },
            )
        )
        world.store.commit()
        if independent_import:

            async def import_first() -> None:
                async with world.client.app.state.business_sessions.begin() as db:
                    await AccountingService(
                        db, DemoAccountingAdapter(db, world.connection_id)
                    ).recheck_invoice(world.ctx, "INV-101")

            asyncio.run(import_first())
        with workers(world, crash="domain"):
            wait_action(world, ref.action_id, "failed" if independent_import else "succeeded")
        action = world.action(ref.action_id)
        invoice = world.store.get(InvoiceSnapshot, world.invoice_id, populate_existing=True)
        assert invoice and (invoice.source_revision, invoice.due_date, invoice.balance_minor) == (
            "2",
            date(2026, 12, 30),
            9000,
        )
        assert action.source_revision == "1"  # The immutable grant was never rewritten.
        original_source = world.store.get(DemoSourceRecord, source.id)
        assert original_source and original_source.payload == original_payload
        revisions = list(
            world.store.scalars(
                select(InvoiceSourceRevision)
                .where(InvoiceSourceRevision.invoice_id == world.invoice_id)
                .order_by(InvoiceSourceRevision.source_revision)
            )
        )
        assert [r.source_revision for r in revisions] == ["1", "2"]
        payments = list(
            world.store.scalars(
                select(VerifiedPaymentAllocation).where(
                    VerifiedPaymentAllocation.invoice_id == world.invoice_id
                )
            )
        )
        assert len(payments) == 1 and payments[0].minor_units == 1000 and payments[0].kind == "cash"
        event = world.store.scalar(
            select(AccountingEvent).where(
                AccountingEvent.invoice_id == world.invoice_id, AccountingEvent.source_revision == "2"
            )
        )
        assert event and event.origin == "AUTHORITATIVE_READ" and event.external_event_id is None
        usage = list(
            world.store.scalars(select(UsageAttempt).where(UsageAttempt.actor_id == action.worker_actor_id))
        )
        if independent_import:
            assert action.last_error == "STALE_ACTION_SCOPE" and action.attempts == 0
            assert usage == []
        else:
            assert sorted((a.operation, a.outcome, a.business_outcome) for a in usage) == [
                ("get_invoice", "VERIFIED", "COMMITTED"),
                ("list_customers", "VERIFIED", "COMMITTED"),
                ("list_payments", "VERIFIED", "COMMITTED"),
            ]
            observation = world.store.scalar(
                select(AccountingObservation).where(AccountingObservation.actor_id == action.worker_actor_id)
            )
            assert observation
            stamp = observation.invoice_data["stamp"]
            amount = observation.payment_data[0]["amount"]
            assert isinstance(stamp, dict) and stamp["revision"] == "2"
            assert isinstance(amount, dict) and amount["minor_units"] == 1000
            assert world.executions(ref.action_id)[0].status == "succeeded"
