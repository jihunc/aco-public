"""Legacy module must use the same trusted composition before opening its listener."""

import os
import runpy
from typing import Any

import pytest
from fastapi import FastAPI

from aco.core import application
from aco.core.settings import Settings

SELECTORS = (
    "ACO_AGENT_LIVE_CONFIG_PATH",
    "ACO_AGENT_LIVE_GRANT_ID",
    "ACO_EMAIL_LIVE_CONFIG_PATH",
    "ACO_VOICE_LIVE_CONFIG_PATH",
)


@pytest.mark.parametrize("selected", [False, True])
def test_module_forwards_typed_settings_through_shared_composition(
    monkeypatch: pytest.MonkeyPatch, selected: bool
) -> None:
    for name in SELECTORS:
        monkeypatch.delenv(name, raising=False)
    if selected:
        for name in SELECTORS:
            monkeypatch.setenv(
                name,
                "/private/tmp/explicit-metadata-only" if name.endswith("PATH") else "explicit-grant-metadata",
            )
    app = FastAPI()
    observed: list[Settings] = []
    listeners: list[tuple[object, dict[str, Any]]] = []

    def compose(settings: Settings) -> FastAPI:
        observed.append(settings)
        assert bool(os.environ.get("ACO_AGENT_LIVE_GRANT_ID")) is selected
        assert (settings.voice_live_config_path is not None) is selected
        return app

    monkeypatch.setattr(application, "configured_app", compose)
    monkeypatch.setattr("uvicorn.run", lambda value, **options: listeners.append((value, options)))
    runpy.run_module("aco.api", run_name="__main__")
    assert len(observed) == 1
    assert observed[0].kill_switch and not observed[0].outbound_enabled
    assert listeners == [(app, {"host": "0.0.0.0", "port": 58000, "access_log": False})]


@pytest.mark.parametrize("partial", ["ACO_AGENT_LIVE_CONFIG_PATH", "ACO_AGENT_LIVE_GRANT_ID"])
def test_module_partial_selection_refuses_before_listener_without_private_reads(
    monkeypatch: pytest.MonkeyPatch, partial: str
) -> None:
    for name in SELECTORS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(partial, "/private/tmp/not-read" if partial.endswith("PATH") else "invalid-grant")

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Partial selection must not open a listener")

    monkeypatch.setattr("uvicorn.run", forbidden)
    with pytest.raises(RuntimeError, match="^BOUNDED_LIVE_CONFIGURATION_BLOCKED$"):
        runpy.run_module("aco.api", run_name="__main__")
