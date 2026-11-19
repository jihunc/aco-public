"""Actual entrypoints refuse incomplete explicit selection before starting a listener/job."""

import sys

import pytest
from pydantic import SecretStr

from aco import cli
from aco.core.settings import Settings
from aco.worker import __main__ as worker_main
from aco.worker.app import WorkerRuntime


@pytest.mark.parametrize("entrypoint", ["api", "worker"])
@pytest.mark.parametrize("selection", ["path", "grant", "invalid"])
def test_partial_or_invalid_provider_selection_never_falls_back(
    monkeypatch: pytest.MonkeyPatch, entrypoint: str, selection: str
) -> None:
    settings = Settings(database_url=SecretStr("postgresql+psycopg://unused:unused@127.0.0.1:1/unused"))
    monkeypatch.setattr(cli, "runtime_settings", lambda: settings)
    monkeypatch.setattr(worker_main, "runtime_settings", lambda: settings)
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)

    async def no_worker(self: WorkerRuntime) -> None:
        await self.engine.dispose()

    monkeypatch.setattr(WorkerRuntime, "run", no_worker)
    for key in ("ACO_AGENT_LIVE_CONFIG_PATH", "ACO_AGENT_LIVE_GRANT_ID", "ACO_EMAIL_LIVE_CONFIG_PATH"):
        monkeypatch.delenv(key, raising=False)
    if selection in {"path", "invalid"}:
        monkeypatch.setenv("ACO_AGENT_LIVE_CONFIG_PATH", "/private/tmp/nonexistent-explicit-operator-config")
    if selection in {"grant", "invalid"}:
        monkeypatch.setenv("ACO_AGENT_LIVE_GRANT_ID", "invalid-grant")
    monkeypatch.setattr(sys, "argv", ["aco", "api"])
    with pytest.raises(RuntimeError, match="^BOUNDED_LIVE_CONFIGURATION_BLOCKED$"):
        (cli.main if entrypoint == "api" else worker_main.main)()


def test_explicit_email_configuration_cannot_disappear_without_api_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "runtime_settings", lambda: Settings())
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "argv", ["aco", "api"])
    monkeypatch.delenv("ACO_AGENT_LIVE_CONFIG_PATH", raising=False)
    monkeypatch.delenv("ACO_AGENT_LIVE_GRANT_ID", raising=False)
    monkeypatch.setenv("ACO_EMAIL_LIVE_CONFIG_PATH", "/private/tmp/nonexistent-explicit-email-config")
    with pytest.raises(RuntimeError, match="^EXPLICIT_PROVIDER_CONFIGURATION_BLOCKED$"):
        cli.main()


def test_default_entrypoint_ignores_credentials_without_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    from typing import NoReturn

    from aco.core.application import configured_app

    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        pytest.fail("Default startup must not read private provider configuration")

    monkeypatch.setattr("aco.integrations.openai.application.load_config", forbidden)
    monkeypatch.setattr("aco.integrations.resend.application.local_email_runtime", forbidden)
    app = configured_app(
        Settings(), {"OPENAI_API_KEY": "synthetic-unused", "RESEND_API_KEY": "synthetic-unused"}
    )
    assert not hasattr(app.state, "agent_live_grant_id")


def test_browser_configuration_clears_every_live_selector() -> None:
    from pathlib import Path

    config = Path("playwright.config.ts").read_text()
    for name in (
        "ACO_AGENT_LIVE_CONFIG_PATH",
        "ACO_AGENT_LIVE_GRANT_ID",
        "ACO_EMAIL_LIVE_CONFIG_PATH",
        "ACO_VOICE_LIVE_CONFIG_PATH",
        "ACO_LIVE_AUTHORIZATION_REF",
        "ACO_LIVE_CAMPAIGN_FILE",
        "ACO_LIVE_ALLOCATION_ID",
    ):
        assert f'{name}: ""' in config
