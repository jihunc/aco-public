"""Normal entrypoints must validate explicitly selected Voice before any listener/job."""

import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from aco import cli
from aco.core.settings import Settings
from aco.worker import __main__ as worker_main


@pytest.mark.parametrize("entrypoint", ["api", "worker"])
def test_invalid_selected_voice_never_starts_listener_or_jobs(
    monkeypatch: pytest.MonkeyPatch, entrypoint: str
) -> None:
    from aco.core.application import configured_app, configured_worker

    settings = Settings(database_url=SecretStr("postgresql+psycopg://unused:unused@127.0.0.1:1/unused"))
    monkeypatch.setattr(cli, "runtime_settings", lambda: settings)
    monkeypatch.setattr(worker_main, "runtime_settings", lambda: settings)
    for name in ("ACO_AGENT_LIVE_CONFIG_PATH", "ACO_AGENT_LIVE_GRANT_ID", "ACO_EMAIL_LIVE_CONFIG_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ACO_VOICE_LIVE_CONFIG_PATH", "/private/tmp/nonexistent-explicit-voice-config")
    monkeypatch.setattr(sys, "argv", ["aco", "api"])

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Invalid Voice selection cannot start a listener or jobs")

    monkeypatch.setattr("uvicorn.run", forbidden)
    if entrypoint == "api":
        with pytest.raises(RuntimeError, match="^EXPLICIT_PROVIDER_CONFIGURATION_BLOCKED$"):
            configured_app(settings)
        with pytest.raises(RuntimeError, match="^EXPLICIT_PROVIDER_CONFIGURATION_BLOCKED$"):
            cli.main()
    else:
        with pytest.raises(ValueError, match="^VOICE_PRIVATE_CONFIGURATION_REQUIRED$"):
            configured_worker(settings)
        with pytest.raises(ValueError, match="^VOICE_PRIVATE_CONFIGURATION_REQUIRED$"):
            worker_main.main()


def test_default_entrypoint_never_reads_voice_private_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    from aco.core.application import configured_app

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Default startup cannot read private Voice configuration")

    monkeypatch.setattr("aco.integrations.vapi.application.read_private", forbidden)
    app = configured_app(Settings(), {"VAPI_API_KEY": "explicit-unused-synthetic-value"})
    assert not hasattr(app.state, "voice_runtime")


@pytest.mark.parametrize("entrypoint", ["api", "worker"])
def test_typed_voice_path_cannot_be_replaced_by_mapping(entrypoint: str) -> None:
    from aco.core.application import configured_app, configured_worker

    settings = Settings(voice_live_config_path=Path("/private/tmp/original-voice-selection"))
    with pytest.raises(ValueError, match="^VOICE_PRIVATE_CONFIGURATION_REQUIRED$"):
        (configured_app if entrypoint == "api" else configured_worker)(
            settings, {"ACO_VOICE_LIVE_CONFIG_PATH": "/private/tmp/replaced-voice-selection"}
        )
