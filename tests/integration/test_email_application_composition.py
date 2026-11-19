"""Explicit application/worker selection; factory authority is verified separately."""

import asyncio
import sys
from dataclasses import replace
from pathlib import Path
from typing import NoReturn

import pytest
from artifact_support import storage_configuration
from fastapi import FastAPI
from identity_support import actor_client

from aco import cli
from aco.api.app import create_app
from aco.core.settings import resolve_ports
from aco.features.communications.provider_ingress import ProviderIngressRuntime
from aco.features.communications.provider_outbound import ProviderOutboundRuntime
from aco.integrations.resend.application import EmailApplicationRuntime
from aco.integrations.s3.adapter import StorageRuntime
from aco.worker import __main__ as worker_main
from aco.worker.app import WorkerRuntime
from aco.worker.jobs import DomainExecutor


def test_api_and_worker_select_only_explicit_email_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object) -> NoReturn:
        pytest.fail("Composition must not invoke provider capability, credentials or wire")

    with actor_client("owner") as (client, store, user, tenant):
        app = client.app
        assert isinstance(app, FastAPI)
        settings = app.state.settings
        sessions = app.state.business_sessions
        storage = StorageRuntime(**storage_configuration())
        ports = replace(resolve_ports(settings), storage=storage)
        ingress = ProviderIngressRuntime(sessions, forbidden, forbidden)
        outbound = ProviderOutboundRuntime(sessions, settings, ingress, forbidden, forbidden)
        selected = EmailApplicationRuntime(ingress, outbound, {})
        seen: list[Path] = []
        path = Path("/private/tmp/explicit-not-read-email-config.json")

        def load(*args: object, config_path: Path | None = None) -> EmailApplicationRuntime:
            assert config_path == path
            seen.append(config_path)
            return selected

        monkeypatch.setattr("aco.integrations.resend.application.local_email_runtime", load)
        monkeypatch.setattr("aco.api.app.resolve_ports", lambda settings: ports)
        monkeypatch.setattr("aco.worker.app.resolve_ports", lambda settings: ports)
        default = create_app(settings)
        assert default.state.email_ingress.provider_ingress is None
        assert seen == []
        actual = create_app(settings, email_config_path=path)
        assert actual.state.ports.email is outbound
        assert actual.state.email_ingress.provider_ingress is ingress
        assert actual.state.email_ingress.shared_webhooks == {}
        worker = WorkerRuntime(settings, email_config_path=path)
        assert isinstance(worker.executor, DomainExecutor)
        assert worker.executor.reminders is not None
        assert worker.executor.reminders.email_runtime is outbound
        assert seen == [path, path]
        asyncio.run(worker.engine.dispose())
        # Exercise the public process entrypoints, not only direct constructor injection.
        for key in ("ACO_AGENT_LIVE_CONFIG_PATH", "ACO_AGENT_LIVE_GRANT_ID"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("ACO_EMAIL_LIVE_CONFIG_PATH", str(path))
        monkeypatch.setattr(cli, "runtime_settings", lambda: settings)
        monkeypatch.setattr(worker_main, "runtime_settings", lambda: settings)
        monkeypatch.setattr(sys, "argv", ["aco", "api"])

        def serve(app: FastAPI, **kwargs: object) -> None:
            assert app.state.ports.email is outbound
            assert app.state.email_ingress.provider_ingress is ingress

        async def work(runtime: WorkerRuntime) -> None:
            assert isinstance(runtime.executor, DomainExecutor)
            assert runtime.executor.reminders is not None
            assert runtime.executor.reminders.email_runtime is outbound
            await runtime.engine.dispose()

        monkeypatch.setattr("uvicorn.run", serve)
        monkeypatch.setattr(WorkerRuntime, "run", work)
        cli.main()
        worker_main.main()
        assert seen == [path, path, path, path]
