"""Separate maintained worker process. No API or browser lifecycle owns durable execution."""

import asyncio
import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from procrastinate import App, PsycopgConnector
from procrastinate.job_context import JobContext
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from aco.core.models import register_models
from aco.core.settings import FakePort, Settings, load_settings, resolve_ports
from aco.features.actions.outbox import OutboxDispatcher
from aco.features.agent.ingress import produce_inbound_run
from aco.features.agent.nodes import ModelFactory
from aco.features.agent.usage import LiveBudgetGate
from aco.features.artifacts.service import ArtifactService
from aco.features.communications.inbound import AgentProducer, execute_email_ingress
from aco.features.communications.provider_ingress import ProviderIngressRuntime
from aco.worker.jobs import ActionExecutor, DomainExecutor, execute_action
from aco.worker.recovery import recover_stalled

if TYPE_CHECKING:
    from aco.features.voice.runtime import VoiceRuntime


class WorkerRuntime:
    def __init__(
        self,
        settings: Settings,
        executor: ActionExecutor | None = None,
        *,
        agent_model_factory: ModelFactory | None = None,
        agent_budget: LiveBudgetGate | None = None,
        ingress_producer: AgentProducer | None = produce_inbound_run,
        ingress_artifacts: ArtifactService | None = None,
        provider_ingress_factory: Callable[[async_sessionmaker[AsyncSession]], ProviderIngressRuntime]
        | None = None,
        email_config_path: Path | None = None,
        voice_runtime_factory: Callable[[async_sessionmaker[AsyncSession]], "VoiceRuntime"] | None = None,
    ):
        register_models()
        Settings.model_validate(settings.model_dump())
        if settings.database_url is None:
            raise ValueError("Worker database configuration required")
        url = settings.database_url.get_secret_value()
        parsed = make_url(url)
        conninfo = parsed.set(drivername="postgresql").render_as_string(hide_password=False)
        self.engine = create_async_engine(url, poolclass=NullPool)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        voice_runtime = voice_runtime_factory(self.sessions) if voice_runtime_factory else None
        if voice_runtime is None and settings.synthetic_voice_enabled:
            from aco.features.voice.demo import preparation_runtime

            voice_runtime = preparation_runtime(
                self.sessions, capability_secret=settings.session_secret, environment=settings.app_env
            )
        provider_ingress = provider_ingress_factory(self.sessions) if provider_ingress_factory else None
        ports = resolve_ports(settings)
        selected_email = None
        if email_config_path is not None:
            if provider_ingress_factory is not None or executor is not None:
                raise ValueError("EMAIL_RUNTIME_CONFIGURATION_CONFLICT")
            from aco.integrations.resend.application import local_email_runtime

            selected_email = local_email_runtime(
                settings,
                self.sessions,
                None if isinstance(ports.storage, FakePort) else ports.storage,
                config_path=email_config_path,
            )
            if selected_email is None:
                raise RuntimeError("EXPLICIT_EMAIL_CONFIGURATION_REQUIRED")
            provider_ingress = selected_email.ingress_runtime
            ports = replace(ports, email=selected_email.outbound_runtime)
        artifacts = (
            None
            if isinstance(ports.storage, FakePort)
            else (ingress_artifacts or ArtifactService(self.sessions, ports.storage, settings.app_env))
        )
        if artifacts is not None and settings.app_env in {"local", "demo"}:
            from aco.integrations.fake.voice_artifacts import SyntheticVoiceArtifacts

            artifacts.voice_artifacts = SyntheticVoiceArtifacts()
        if executor is None:
            from aco.features.communications.outbound import ReminderDispatcher
            from aco.integrations.demo_email.adapter import DemoEmailRuntime

            reminders = (
                None
                if isinstance(ports.storage, FakePort)
                else ReminderDispatcher(
                    self.sessions,
                    settings,
                    selected_email.outbound_runtime
                    if selected_email is not None
                    else DemoEmailRuntime(self.sessions),
                    ports.storage,
                )
            )
            executor = DomainExecutor(reminders)
        self.executor = executor
        self.app = App(connector=PsycopgConnector(conninfo=conninfo))
        self.dispatcher = OutboxDispatcher(self.sessions, self.app)
        from aco.features.agent.graph import (
            dispatch_agent_runs,
            execute_agent_run,
            prepare_agent_checkpoints,
            recover_agent_runs,
        )

        checkpoints_ready = False
        checkpoint_lock = asyncio.Lock()

        async def prepare_checkpoints() -> None:
            nonlocal checkpoints_ready
            async with checkpoint_lock:
                if not checkpoints_ready:
                    await prepare_agent_checkpoints(conninfo)
                    checkpoints_ready = True

        @self.app.task(name="aco.investigate", queue="aco", pass_context=True, retry=False)
        async def investigate(job: JobContext, run_id: str, expected_version: int) -> None:
            assert job.job.id is not None
            try:
                await prepare_checkpoints()
            except Exception:
                from aco.features.agent.events import append_event
                from aco.features.agent.models import AgentRun

                async with self.sessions.begin() as db:
                    run = await db.get(AgentRun, UUID(run_id), with_for_update=True)
                    if (
                        run is not None
                        and run.status == "queued"
                        and run.job_id == job.job.id
                        and run.job_version == expected_version
                    ):
                        run.status, run.error_code = "failed", "CHECKPOINT_BOOTSTRAP_UNAVAILABLE"
                        run.version += 1
                        await db.run_sync(
                            lambda store: append_event(
                                store, run, "failed", "Investigation checkpoint initialization unavailable"
                            )
                        )
                raise RuntimeError("CHECKPOINT_BOOTSTRAP_UNAVAILABLE") from None
            await execute_agent_run(
                self.sessions,
                conninfo,
                UUID(run_id),
                expected_version,
                job.job.id,
                agent_model_factory,
                agent_budget,
                voice_runtime,
            )

        from aco.features.communications.ingress import (
            current_activation,
            dispatch_email_ingress,
            recover_email_ingress,
        )

        @self.app.task(name="aco.ingest_email", queue="aco", pass_context=True, retry=False)
        async def ingest_email(job: JobContext, receipt_id: str, expected_version: int) -> None:
            from hashlib import sha256

            from fastapi import HTTPException

            from aco.features.communications.models import EmailIngressReceipt

            assert job.job.id is not None
            key = UUID(receipt_id)
            async with self.sessions.begin() as db:
                row = await db.get(EmailIngressReceipt, key)
                if (
                    row is None
                    or row.state != "queued"
                    or row.job_id != job.job.id
                    or row.job_version != expected_version
                ):
                    return
                error = None
                try:
                    activation = await db.run_sync(
                        lambda store: current_activation(store, row.activation_id, row.tenant_id)
                    )
                    secret = settings.synthetic_email_ingress_webhook_secret
                    if activation.provider_binding is not None:
                        if provider_ingress is None:
                            error = "PROVIDER_INGRESS_DISABLED"
                        else:
                            await db.run_sync(lambda store: provider_ingress.validate(store, activation))
                        if artifacts is None:
                            error = "INGRESS_STORAGE_UNAVAILABLE"
                    elif not settings.synthetic_email_ingress_enabled:
                        error = "SYNTHETIC_INGRESS_DISABLED"
                    elif artifacts is None:
                        error = "INGRESS_STORAGE_UNAVAILABLE"
                    elif (
                        secret is None
                        or activation.secret_sha256 != sha256(secret.get_secret_value().encode()).hexdigest()
                    ):
                        error = "INGRESS_CONFIGURATION_CHANGED"
                except HTTPException as exc:
                    error = str(exc.detail)
                claimed = await db.get(EmailIngressReceipt, key, with_for_update=True, populate_existing=True)
                assert claimed is not None
                if (
                    claimed.state != "queued"
                    or claimed.job_id != job.job.id
                    or claimed.job_version != expected_version
                ):
                    return
                if error is not None:
                    claimed.state, claimed.error_code = "failed", error
                    claimed.version += 1
                    return
            assert artifacts is not None
            await execute_email_ingress(
                self.sessions,
                key,
                expected_version,
                job.job.id,
                artifacts,
                ingress_producer,
                provider_ingress,
            )

        @self.app.task(name="aco.execute", queue="aco", pass_context=True, retry=False)
        async def execute(job: JobContext, action_id: str, expected_version: int) -> None:
            await execute_action(
                self.sessions, UUID(action_id), expected_version, self.executor, job_id=job.job.id
            )

        @self.app.task(name="aco.purge_artifact", queue="aco", pass_context=True, retry=False)
        async def purge_artifact(job: JobContext, purge_id: str, expected_version: int) -> None:
            from aco.features.artifacts.retention import execute_purge

            if artifacts is None:
                raise RuntimeError("ARTIFACT_STORAGE_UNAVAILABLE")
            assert job.job.id is not None
            await execute_purge(self.sessions, UUID(purge_id), expected_version, job.job.id, artifacts)

        @self.app.periodic(cron="* * * * * *")
        @self.app.task(name="aco.maintenance", queue="aco", retry=False)
        async def maintenance(timestamp: int) -> None:
            from aco.features.artifacts.retention import dispatch_purges, recover_purges

            await recover_purges(self.sessions)
            if artifacts is not None:
                await dispatch_purges(self.sessions, self.app)
            await recover_stalled(self.app, self.sessions)
            await self.dispatcher.dispatch_outbox(50)
            await recover_email_ingress(self.sessions)
            await dispatch_email_ingress(self.sessions, self.app)
            await recover_agent_runs(self.sessions, self.app)
            await dispatch_agent_runs(self.sessions, self.app)

    async def run(self) -> None:
        try:
            async with self.app.open_async():
                await self.app.run_worker_async(
                    queues=["aco"],
                    concurrency=2,
                    listen_notify=False,
                    update_heartbeat_interval=1,
                    stalled_worker_timeout=3,
                    fetch_job_polling_interval=0.1,
                )
        finally:
            await self.engine.dispose()


if __name__ == "__main__":
    from aco.core.application import configured_worker

    asyncio.run(configured_worker(load_settings(os.environ)).run())
