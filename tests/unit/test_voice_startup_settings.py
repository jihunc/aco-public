"""Voice selector metadata admits validation, never a provider or capture authority."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from aco.core.local_runtime import local_provider_selection, local_settings
from aco.core.settings import load_settings, resolve_ports


@pytest.mark.parametrize("environment", ["local", "demo"])
def test_standalone_voice_selection_reaches_typed_configuration_without_private_reads(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    def no_read(*args: object, **kwargs: object) -> str:
        raise AssertionError("Settings must not read private configuration")

    monkeypatch.setattr(Path, "read_text", no_read)
    settings = load_settings(
        {
            "APP_ENV": environment,
            "WEB_PUBLIC_URL": "https://aco.invalid",
            "ACO_VOICE_LIVE_CONFIG_PATH": "/private/tmp/voice-config-not-created.json",
            "OUTBOUND_ENABLED": "true",
            "AUTOMATION_KILL_SWITCH": "false",
        }
    )
    assert settings.voice_live_config_path == Path("/private/tmp/voice-config-not-created.json")
    assert not settings.synthetic_email_enabled
    assert not settings.synthetic_voice_enabled
    assert not settings.recording_enabled and not settings.transcription_enabled
    assert resolve_ports(settings).provider_modes()["voice"] == "fake"
    assert "voice-config-not-created" not in repr(settings)


@pytest.mark.parametrize(
    "extra",
    [
        {"APP_ENV": "production", "WEB_PUBLIC_URL": "https://aco.invalid"},
        {"ACO_VOICE_LIVE_CONFIG_PATH": "relative.json"},
        {"ACO_VOICE_LIVE_CONFIG_PATH": ""},
        {"VOICE_ADAPTER": "vapi"},
        {"RECORDING_ENABLED": "true"},
        {"TRANSCRIPTION_ENABLED": "true"},
    ],
)
def test_voice_metadata_never_bypasses_other_settings_guards(extra: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        load_settings(
            {
                "ACO_VOICE_LIVE_CONFIG_PATH": "/private/tmp/voice-config-not-created.json",
                "OUTBOUND_ENABLED": "true",
                "AUTOMATION_KILL_SWITCH": "false",
                **extra,
            }
        )


def test_local_launcher_preserves_explicit_voice_metadata_only() -> None:
    source = {
        "ACO_VOICE_LIVE_CONFIG_PATH": "/private/tmp/voice-config-not-created.json",
        "OUTBOUND_ENABLED": "true",
        "AUTOMATION_KILL_SWITCH": "false",
    }
    settings = local_settings(source)
    assert settings.voice_live_config_path == Path(source["ACO_VOICE_LIVE_CONFIG_PATH"])
    assert (
        local_provider_selection(source)["ACO_VOICE_LIVE_CONFIG_PATH"] == source["ACO_VOICE_LIVE_CONFIG_PATH"]
    )
    defaults = load_settings({})
    assert defaults.voice_live_config_path is None
    assert defaults.kill_switch and not defaults.outbound_enabled
