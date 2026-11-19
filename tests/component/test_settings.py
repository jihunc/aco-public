"""Catch unsafe activation, untyped settings and registry vendor fallbacks."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aco.core.clock import FrozenClock
from aco.core.settings import load_settings, resolve_ports
from aco.core.types import Money, SourceStamp


def test_live_profile_cannot_be_selected_by_default() -> None:
    settings = load_settings({})
    assert settings.external_providers == "fake"
    assert settings.outbound_enabled is False
    assert settings.kill_switch is True
    assert settings.recording_enabled is False
    assert settings.transcription_enabled is False
    registry = resolve_ports(settings)
    assert registry.provider_modes() == {
        name: "fake" for name in ("accounting", "email", "voice", "llm", "storage", "identity", "telephony")
    }
    assert settings.test_profile == "mock"


@pytest.mark.parametrize(
    "env",
    [
        {"EXTERNAL_PROVIDERS": "live"},
        {"OUTBOUND_ENABLED": "true"},
        {"AUTOMATION_KILL_SWITCH": "false"},
        {"RECORDING_ENABLED": "true"},
        {"TRANSCRIPTION_ENABLED": "true"},
        {"LLM_PROVIDER": "openai"},
        {"ACO_TEST_PROFILE": "live"},
    ],
)
def test_missing_required_live_setting_blocks_activation(env: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        load_settings(env)


def test_clock_and_canonical_money_source_reject_lossy_values() -> None:
    frozen = datetime(2026, 11, 17, tzinfo=UTC)
    assert FrozenClock(frozen).now() == frozen
    with pytest.raises(ValidationError):
        Money.model_validate({"minor_units": 1.5, "currency": "AUD", "exponent": 2})
    with pytest.raises(ValidationError):
        Money(minor_units=1, currency="aud", exponent=2)
    with pytest.raises(ValidationError):
        SourceStamp(
            connection_id=uuid4(),
            external_id="x",
            observed_at=datetime(2026, 1, 1),
            source_at=None,
            revision=None,
            synthetic=True,
        )


def test_frozen_clock_rejects_naive_instants() -> None:
    with pytest.raises(ValueError, match="timezone"):
        FrozenClock(datetime(2026, 11, 17))


@pytest.mark.parametrize(
    "env",
    [
        {
            "APP_ENV": "production",
            "WEB_PUBLIC_URL": "https://aco.invalid",
            "OIDC_ISSUER": "http://identity.invalid/aco",
            "OIDC_DISCOVERY_URL": "http://identity.invalid/aco/.well-known/openid-configuration",
        },
        {"WEB_PUBLIC_URL": "https://aco.invalid/onboarding"},
        {"SESSION_SECRET": "short"},
    ],
)
def test_identity_transport_origin_and_signing_secret_fail_closed(env: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        load_settings(env)
