"""Real synthetic activation/queue setup; no positive tickets or provider effects are seeded."""

from contextlib import contextmanager
from dataclasses import replace
from typing import Any
from uuid import UUID

from artifact_support import storage_configuration
from policy_support import activate, configure_recipients, policy_world, save
from sqlalchemy import select, text

from aco.core.settings import Settings
from aco.features.identity.models import ProviderConnection
from aco.integrations.s3.adapter import StorageRuntime


@contextmanager
def reminder_world(request_cap: Any = 30) -> Any:
    with policy_world() as (client, store, ctx, invoices, saved):
        runtime = StorageRuntime(**storage_configuration())
        storage = ProviderConnection(
            tenant_id=ctx.tenant_id,
            provider_type="storage",
            provider="s3",
            status="READY",
            external_account_id="synthetic-t10",
        )
        store.add(storage)
        store.commit()
        app = client.app
        settings = Settings.model_validate(
            app.state.settings.model_dump()
            | {"synthetic_email_enabled": True, "outbound_enabled": True, "kill_switch": False}
        )
        app.state.settings = settings
        from aco.integrations.demo_email.adapter import DemoEmailRuntime

        app.state.ports = replace(
            app.state.ports, storage=runtime, email=DemoEmailRuntime(app.state.business_sessions)
        )
        from aco.features.accounting.models import CustomerContact

        destinations = list(
            store.scalars(select(CustomerContact.email).where(CustomerContact.tenant_id == ctx.tenant_id))
        )
        store.rollback()
        response = client.put(
            f"/api/workspaces/{ctx.tenant_id}/email-execution",
            json={
                "expected_activation_id": None,
                "sender": "collections@aco.example.test",
                "destinations": destinations,
                "request_cap": request_cap,
                "synthetic_capture_reviewed": True,
                "sender_and_destinations_reviewed": True,
                "evidence": "Owner reviewed synthetic local email and 30 day capture",
            },
        )
        assert response.status_code == 200, response.text
        configure_recipients(client, store, ctx)
        definition = dict(saved["definition"])
        definition["rules"] = [{**definition["rules"][0], "offset_days": 120}]
        saved = save(
            client,
            ctx.tenant_id,
            policy_id=saved["policy_id"],
            name=saved["name"],
            expected_version=saved["version"],
            definition=definition,
        )
        saved = activate(client, ctx, saved)
        from datetime import UTC, datetime

        from aco.features.policies.models import RecipientControl

        offset = 12 - datetime.now(UTC).hour
        zone = "Etc/GMT" if offset == 0 else f"Etc/GMT{'-' if offset > 0 else '+'}{abs(offset)}"

        contacts = list(
            store.scalars(select(CustomerContact).where(CustomerContact.tenant_id == ctx.tenant_id))
        )
        for contact in contacts:
            control = store.scalar(select(RecipientControl).where(RecipientControl.contact_id == contact.id))
            assert control is not None
            response = client.put(
                f"/api/workspaces/{ctx.tenant_id}/policy-recipients/{contact.id}",
                json={
                    "expected_version": control.version,
                    "expected_contact_revision": contact.source_revision,
                    "expected_destination": contact.email,
                    "timezone": zone,
                    "timezone_verified": True,
                    "owner_confirmed_tenant_timezone": False,
                    "destination_verified": True,
                    "allowed_channels": ["email"],
                    "opt_out": False,
                    "evidence": "Owner reviewed actual synthetic recipient local business-hour zone",
                },
            )
            assert response.status_code == 200, response.text
            store.rollback()
        response = client.post(
            f"/api/workspaces/{ctx.tenant_id}/automation/pause", json={"scope": "tenant", "paused": False}
        )
        assert response.status_code == 200, response.text
        try:
            yield client, store, ctx, invoices, saved
        finally:
            # Cleanup owns only exact canonical keys created in this unique tenant/schema.
            import asyncio

            from aco.features.artifacts.models import Artifact
            from aco.features.artifacts.service import object_ref

            store.rollback()
            artifacts = list(store.scalars(select(Artifact).where(Artifact.tenant_id == ctx.tenant_id)))
            for artifact in artifacts:
                if artifact.storage_state == "stored":
                    asyncio.run(
                        runtime.bind(ctx.tenant_id, artifact.provider_connection_id).delete_object(
                            ctx, object_ref(artifact)
                        )
                    )


def dispatch_one(client: Any, store: Any, ctx: Any, invoice_id: Any = None) -> Any:
    import asyncio

    from aco.features.actions.models import OutboxEvent, ScheduledAction
    from aco.features.communications.outbound import ReminderDispatcher
    from aco.features.identity.models import now
    from aco.worker.app import WorkerRuntime
    from aco.worker.jobs import DomainExecutor

    query = select(ScheduledAction).where(
        ScheduledAction.tenant_id == ctx.tenant_id, ScheduledAction.status == "queued"
    )
    if invoice_id:
        query = query.where(ScheduledAction.invoice_id == invoice_id)
    action = store.scalar(query.order_by(ScheduledAction.created_at.desc()))
    assert action
    action.due_at = now()
    outbox = store.scalar(
        select(OutboxEvent).where(OutboxEvent.action_id == action.id, OutboxEvent.dispatched_at.is_(None))
    )
    if outbox is not None:
        outbox.due_at = action.due_at
    else:
        assert action.job_id is not None, "Published outbox must retain an actual job binding"
    store.commit()
    action_id = action.id
    app = client.app

    async def run() -> Any:
        dispatcher = ReminderDispatcher(
            app.state.business_sessions, app.state.settings, app.state.ports.email, app.state.ports.storage
        )
        runtime = WorkerRuntime(app.state.settings, DomainExecutor(dispatcher))
        try:
            async with runtime.app.open_async():
                job = None
                for _ in range(20):
                    await runtime.dispatcher.dispatch_outbox(100)
                    async with runtime.sessions() as db:
                        current = await db.get(ScheduledAction, action_id)
                        assert current is not None
                        job = current.job_id
                    if job is not None:
                        break
                assert job is not None, "Actual outbox did not publish the selected action"
                assert current is not None
                selected_jobs = list(await runtime.app.job_manager.list_jobs_async(id=job))
                assert len(selected_jobs) == 1
                selected_job = selected_jobs[0]
                assert selected_job.task_kwargs == {
                    "action_id": str(action_id),
                    "expected_version": current.version,
                }
                assert selected_job.status == "todo"
                # The prior maintained worker may already have published this
                # future invoice or retry. Fast-forward the same fixture clock
                # at both layers; never invent a doing job, attempt, or result.
                async with runtime.sessions.begin() as db:
                    advanced = await db.scalar(
                        text(
                            "UPDATE procrastinate_jobs SET scheduled_at=:due "
                            "WHERE id=:job AND status='todo' AND task_name='aco.execute' "
                            "AND args->>'action_id'=:action "
                            "AND (args->>'expected_version')::integer=:version RETURNING id"
                        ),
                        {
                            "due": current.due_at,
                            "job": job,
                            "action": str(action_id),
                            "version": current.version,
                        },
                    )
                    assert advanced == job
                scheduled = list(await runtime.app.job_manager.list_jobs_async(id=job))
                assert len(scheduled) == 1
                assert scheduled[0].scheduled_at == current.due_at
                assert scheduled[0].scheduled_at <= now()
                assert scheduled[0].status == selected_job.status
                assert scheduled[0].attempts == selected_job.attempts
                await runtime.app.run_worker_async(
                    queues=["aco"],
                    concurrency=1,
                    wait=False,
                    listen_notify=False,
                    install_signal_handlers=False,
                )
        finally:
            await runtime.engine.dispose()

    asyncio.run(run())
    store.rollback()
    return store.get(ScheduledAction, action_id, populate_existing=True)


def dispatch_maintained(client: Any, store: Any, ctx: Any) -> Any:
    """Existing maintained worker recipe; never invent doing status/heartbeat."""
    import asyncio

    from aco.features.actions.models import OutboxEvent, ScheduledAction
    from aco.features.communications.outbound import ReminderDispatcher
    from aco.features.identity.models import now
    from aco.worker.app import WorkerRuntime
    from aco.worker.jobs import DomainExecutor

    action = store.scalar(
        select(ScheduledAction)
        .where(ScheduledAction.tenant_id == ctx.tenant_id, ScheduledAction.status == "queued")
        .order_by(ScheduledAction.created_at.desc())
    )
    assert action is not None
    outbox = store.scalar(
        select(OutboxEvent).where(
            OutboxEvent.action_id == action.id,
            OutboxEvent.action_version == action.version,
            OutboxEvent.dispatched_at.is_(None),
        )
    )
    assert outbox is not None
    action.due_at = outbox.due_at = now()
    key: UUID = action.id
    store.commit()
    app = client.app

    async def work() -> None:
        dispatcher = ReminderDispatcher(
            app.state.business_sessions, app.state.settings, app.state.ports.email, app.state.ports.storage
        )
        worker = WorkerRuntime(app.state.settings, DomainExecutor(dispatcher))
        try:
            async with worker.app.open_async():
                for _ in range(20):
                    await worker.dispatcher.dispatch_outbox(100)
                    async with worker.sessions() as db:
                        current = await db.get(ScheduledAction, key)
                        assert current is not None
                        if current.job_id is not None:
                            break
                assert current is not None and current.job_id is not None
                await worker.app.run_worker_async(
                    queues=["aco"],
                    concurrency=1,
                    wait=False,
                    listen_notify=False,
                    install_signal_handlers=False,
                )
        finally:
            await worker.engine.dispose()

    asyncio.run(work())
    store.rollback()
    return store.get(ScheduledAction, key, populate_existing=True)
