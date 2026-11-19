"""Independent synthetic transport state and real spawned production workers."""

import asyncio
import multiprocessing
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID

from accounting_support import context, seed
from identity_support import actor_client
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.core.types import ActionResult, ActionStatus, ProviderErrorCode, ProviderFailure, ProviderReference
from aco.features.identity.models import now


@dataclass
class World:
    client: Any
    store: Session
    ctx: TenantContext
    connection_id: UUID
    invoice_id: UUID
    policy_version_id: UUID
    source_event_id: UUID
    worker_pids: list[int] = field(default_factory=list)

    @property
    def url(self) -> str:
        engine = self.store.get_bind()
        assert isinstance(engine, Engine)
        return engine.url.render_as_string(hide_password=False)

    def action(self, action_id: UUID) -> Any:
        from aco.features.actions.models import ScheduledAction

        self.store.rollback()
        return self.store.get(ScheduledAction, action_id, populate_existing=True)

    def executions(self, action_id: UUID) -> list[Any]:
        from aco.features.actions.models import ActionExecution

        return list(
            self.store.scalars(
                select(ActionExecution)
                .where(ActionExecution.action_id == action_id)
                .order_by(ActionExecution.attempt)
            )
        )

    def effect_count(self, action_id: UUID) -> int:
        self.store.rollback()
        return int(
            self.store.scalar(
                text("SELECT count(*) FROM synthetic_effects WHERE action_id=:id"), {"id": action_id}
            )
        )

    def submission_count(self, action_id: UUID) -> int:
        self.store.rollback()
        return int(
            self.store.scalar(
                text("SELECT count(*) FROM synthetic_submissions WHERE action_id=:id"), {"id": action_id}
            )
        )

    def wait_effect(self, action_id: UUID) -> None:
        deadline = time.monotonic() + 20
        while self.effect_count(action_id) == 0:
            assert time.monotonic() < deadline, "Worker never persisted synthetic provider acceptance"
            time.sleep(0.05)

    def expire_vendor_key(self, action_id: UUID, expired: bool) -> None:
        self.store.execute(
            text("UPDATE synthetic_effects SET key_expired=:value WHERE action_id=:id"),
            {"value": expired, "id": action_id},
        )
        self.store.commit()


@contextmanager
def action_world() -> Iterator[World]:
    from aco.features.accounting.models import AccountingEvent, InvoiceSnapshot
    from aco.features.policies.models import CollectionPolicyVersion

    with actor_client("owner") as (client, store, user, tenant):
        seed(client, tenant)
        invoice = store.scalar(
            select(InvoiceSnapshot).where(
                InvoiceSnapshot.tenant_id == tenant, InvoiceSnapshot.number == "INV-101"
            )
        )
        policy = store.scalar(
            select(CollectionPolicyVersion).where(CollectionPolicyVersion.tenant_id == tenant)
        )
        assert invoice is not None and policy is not None
        event = store.scalar(select(AccountingEvent).where(AccountingEvent.invoice_id == invoice.id))
        assert event is not None
        store.execute(
            text(
                "CREATE TABLE synthetic_submissions (id bigint GENERATED ALWAYS AS IDENTITY, "
                "action_id uuid NOT NULL, at timestamptz NOT NULL DEFAULT clock_timestamp())"
            )
        )
        store.execute(
            text(
                "CREATE TABLE synthetic_effects (action_id uuid PRIMARY KEY, operation_key text "
                "NOT NULL UNIQUE, key_expired boolean NOT NULL DEFAULT false)"
            )
        )
        store.commit()
        yield World(
            client,
            store,
            context(tenant, user.id),
            invoice.provider_connection_id,
            invoice.id,
            policy.id,
            event.id,
        )


def scheduled(world: World, **changes: Any) -> Any:
    from aco.features.actions.service import ScheduledCommand

    values = dict(
        kind="accounting_recheck",
        provider_connection_id=world.connection_id,
        invoice_id=world.invoice_id,
        policy_version_id=world.policy_version_id,
        source_event_id=world.source_event_id,
        due_at=now(),
        payload={},
    )
    return ScheduledCommand.model_validate(values | changes)


def enqueue(world: World, **changes: Any) -> Any:
    from aco.features.actions.service import ActionService

    ref = ActionService(world.store).enqueue_action(world.ctx, scheduled(world, **changes))
    world.store.commit()
    return ref


class SyntheticTransport:
    """Acceptance commits independently, as a real remote provider would."""

    def __init__(
        self,
        url: str,
        outcome: str = "succeeded",
        crash: str = "",
        may_have_side_effect: bool = False,
        retry_after: int = 0,
    ):
        self.engine = create_engine(url)
        self.outcome, self.crash = outcome, crash
        self.possible, self.retry_after = may_have_side_effect, retry_after

    async def execute(self, ctx: Any, action: Any, db: Any) -> ActionResult:
        from aco.features.actions.service import ActionFailure

        if self.crash == "before_acceptance":
            os._exit(72)
        outcome = (
            ("TEMPORARILY_UNAVAILABLE" if action.attempts == 1 else "succeeded")
            if self.crash == "retry_once"
            else self.outcome
        )
        with self.engine.begin() as transport:
            transport.execute(
                text("INSERT INTO synthetic_submissions(action_id) VALUES(:id)"), {"id": action.id}
            )
            if outcome in {"succeeded", "UNKNOWN_SIDE_EFFECT"} or self.possible:
                transport.execute(
                    text(
                        "INSERT INTO synthetic_effects(action_id,operation_key) VALUES(:id,:key) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {"id": action.id, "key": action.operation_key},
                )
        if self.crash == "after_acceptance":
            os._exit(71)
        if outcome != "succeeded":
            possible = self.possible or outcome == "UNKNOWN_SIDE_EFFECT"
            raise ActionFailure(
                ProviderFailure(
                    code=ProviderErrorCode(outcome),
                    retryable=not possible,
                    may_have_side_effect=possible,
                    retry_after=timedelta(seconds=self.retry_after),
                )
            )
        return ActionResult(
            action_id=action.id,
            status=ActionStatus.SUCCEEDED,
            provider_ref=ProviderReference(
                provider="demo", connection_id=action.provider_connection_id, external_id=str(action.id)
            ),
        )


def worker_child(url: str, crash: str) -> None:
    from pydantic import SecretStr

    from aco.core.settings import Settings
    from aco.worker.app import WorkerRuntime
    from aco.worker.jobs import DomainExecutor

    runtime = WorkerRuntime(
        Settings(database_url=SecretStr(url)),
        executor=DomainExecutor() if crash == "domain" else SyntheticTransport(url, crash=crash),
    )
    asyncio.run(runtime.run())


@contextmanager
def workers(world: World, count: int = 1, crash: str = "") -> Iterator[list[Any]]:
    processes = [
        multiprocessing.get_context("spawn").Process(target=worker_child, args=(world.url, crash))
        for _ in range(count)
    ]
    world.store.rollback()
    try:
        for process in processes:
            process.start()
            assert process.pid is not None
            world.worker_pids.append(process.pid)
        print({"worker_pids": world.worker_pids, "executor_mode": crash or "synthetic"}, flush=True)
        yield processes
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(1)
            if process.is_alive():
                process.kill()
                process.join(5)


def wait_action(world: World, action_id: UUID, status: str) -> Any:
    deadline = time.monotonic() + 25
    while True:
        action = world.action(action_id)
        if action.status == status:
            return action
        assert time.monotonic() < deadline, f"Expected {status}; observed {action.status}"
        time.sleep(0.05)


def run_attempts(
    world: World,
    ref: Any,
    outcome: str,
    attempts: int = 3,
    may_have_side_effect: bool = False,
    retry_after: int = 0,
) -> None:
    from pydantic import SecretStr

    from aco.core.settings import Settings
    from aco.worker.app import WorkerRuntime
    from aco.worker.jobs import execute_action

    async def run() -> None:
        runtime = WorkerRuntime(Settings(database_url=SecretStr(world.url)))
        executor = SyntheticTransport(
            world.url, outcome, may_have_side_effect=may_have_side_effect, retry_after=retry_after
        )
        try:
            async with runtime.app.open_async():
                for _ in range(attempts):
                    await runtime.dispatcher.dispatch_outbox(10)
                    action = world.action(ref.action_id)
                    # Inject clock at persisted due time; never rewrite queued state or due dates.
                    await execute_action(
                        runtime.sessions,
                        action.id,
                        action.version,
                        executor,
                        job_id=action.job_id,
                        at=action.due_at,
                    )
        finally:
            executor.engine.dispose()
            await runtime.engine.dispose()

    world.store.rollback()
    asyncio.run(run())
