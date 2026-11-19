"""Explicit development selection cannot invent provider authority or leak private inputs."""

from pathlib import Path

import pytest


def configuration(tmp_path: Path, additions: str = "") -> Path:
    (tmp_path / "providers").mkdir(exist_ok=True, mode=0o700)
    (tmp_path / "campaign").mkdir(exist_ok=True, mode=0o700)
    infra = tmp_path / "infra.env"
    infra.write_text(
        'PRODUCT_PROJECT="aco-product-dev-unit"\nPRODUCT_WEB_PORT="53420"\n'
        'PRODUCT_API_PORT="58420"\nPRODUCT_IDENTITY_PORT="58480"\n'
        'PRODUCT_DATABASE_URL="postgresql+psycopg://aco@postgres:5432/aco"\n'
        'OIDC_CLIENT_SECRET="synthetic-private-identity"\n'
        'SESSION_SECRET="synthetic-session-secret-with-more-than-32-characters"\n'
    )
    infra.chmod(0o600)
    config = tmp_path / "development.env"
    config.write_text(
        f"DEV_MODE=offline\nDEV_INFRA_CONFIG={infra}\n"
        f"DEV_PROVIDER_DIRECTORY={tmp_path / 'providers'}\n"
        f"DEV_CAMPAIGN_DIRECTORY={tmp_path / 'campaign'}\n" + additions
    )
    config.chmod(0o600)
    return config


def test_offline_snapshot_ignores_ambient_credentials_and_no_private_live_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aco.core.development import load_development

    monkeypatch.setenv("OPENAI_API_KEY", "never-forward-this")
    monkeypatch.setenv("ACO_EMAIL_LIVE_CONFIG_PATH", "/never/read/ambient.json")
    config = load_development(configuration(tmp_path))
    settings = config.settings()
    assert settings.app_env == "local" and settings.llm_provider == "fake"
    assert settings.external_providers == "fake" and settings.accounting_adapter == "demo"
    assert not any(config.selection().values())
    assert "never-forward" not in repr(config)
    assert set(config.web_environment()) == {"ACO_API_INTERNAL_ORIGIN", "NEXT_TELEMETRY_DISABLED"}
    assert "OPENAI_API_KEY" not in config.backend_environment()
    assert config.settings() == config.settings()  # same explicit API/worker snapshot


@pytest.mark.parametrize(
    "llm,email,voice",
    [
        ("fake", "mock", "mock"),
        ("openai", "mock", "mock"),
        ("fake", "resend", "mock"),
        ("fake", "mock", "vapi"),
        ("openai", "resend", "vapi"),
        ("openai", "resend", "mock"),
        ("openai", "mock", "vapi"),
        ("fake", "resend", "vapi"),
    ],
)
def test_independent_live_selection_translates_only_existing_loader_paths(
    tmp_path: Path, llm: str, email: str, voice: str
) -> None:
    from aco.core.development import load_development

    config_path = configuration(tmp_path)
    text = config_path.read_text().replace("DEV_MODE=offline", "DEV_MODE=live")
    text += f"LLM_PROVIDER={llm}\nEMAIL_ADAPTER={email}\nVOICE_ADAPTER={voice}\n"
    if llm == "openai":
        text += (
            "ACO_AGENT_LIVE_CONFIG_PATH=/run/aco/providers/openai.env\n"
            "ACO_AGENT_LIVE_GRANT_ID=00000000-0000-4000-8000-000000000001\n"
            "ACO_LIVE_AUTHORIZATION_REF=operator-reference\n"
            "ACO_LIVE_CAMPAIGN_FILE=/var/lib/aco/live/live-delivery-2026-11-19.sqlite3\n"
            "ACO_LIVE_ALLOCATION_ID=00000000-0000-4000-8000-000000000002\n"
        )
    if email == "resend":
        text += "ACO_EMAIL_LIVE_CONFIG_PATH=/run/aco/providers/email.json\n"
    if voice == "vapi":
        text += "ACO_VOICE_LIVE_CONFIG_PATH=/run/aco/providers/voice.json\n"
    config_path.write_text(text)
    config = load_development(config_path)
    assert bool(config.selection()["ACO_AGENT_LIVE_CONFIG_PATH"]) == (llm == "openai")
    assert bool(config.selection()["ACO_EMAIL_LIVE_CONFIG_PATH"]) == (email == "resend")
    assert bool(config.selection()["ACO_VOICE_LIVE_CONFIG_PATH"]) == (voice == "vapi")
    assert config.settings().external_providers == "fake"
    assert config.settings().synthetic_voice_enabled == (voice == "mock")
    assert config.settings().kill_switch  # selection is not contact/spend authority
    assert "operator" not in repr(config)


@pytest.mark.parametrize(
    "extra",
    [
        "LLM_PROVIDER=openai\n",
        "EMAIL_ADAPTER=resend\n",
        "VOICE_ADAPTER=vapi\n",
        "ACO_AGENT_LIVE_CONFIG_PATH=/private/tmp/hidden\n",
        "APP_ENV=production\n",
        "EXTERNAL_PROVIDERS=live\n",
        "ACCOUNTING_ADAPTER=xero\n",
        "OPENAI_API_KEY=must-not-be-a-frontend-secret\n",
        "DEV_DATABASE_PORT=53420\n",
        "DEV_DATABASE_PORT=bad\n",
        "LLM_PROVIDER=fake\nLLM_PROVIDER=openai\n",
        "DEV_EXTRA=${AMBIENT_SECRET}\n",
        "ACO_EMAIL_LIVE_CONFIG_PATH=relative.json\n",
    ],
)
def test_invalid_or_contradictory_config_fails_closed_without_echoing_inputs(
    tmp_path: Path, extra: str
) -> None:
    from aco.core.development import load_development

    path = configuration(tmp_path, extra)
    with pytest.raises(ValueError, match="^DEVELOPMENT_CONFIGURATION_BLOCKED$"):
        load_development(path)


def test_live_partial_provider_paths_cannot_silently_fall_back(tmp_path: Path) -> None:
    from aco.core.development import load_development

    path = configuration(tmp_path, "LLM_PROVIDER=openai\n")
    path.write_text(path.read_text().replace("DEV_MODE=offline", "DEV_MODE=live"))
    with pytest.raises(ValueError, match="^DEVELOPMENT_CONFIGURATION_BLOCKED$"):
        load_development(path)


@pytest.mark.parametrize("kind", ["mode", "symlink", "infra-mode", "relative-infra", "interpolation"])
def test_private_file_boundaries(tmp_path: Path, kind: str) -> None:
    from aco.core.development import load_development

    path = configuration(tmp_path)
    if kind == "mode":
        path.chmod(0o644)
    elif kind == "symlink":
        link = tmp_path / "alias.env"
        link.symlink_to(path)
        path = link
    elif kind == "infra-mode":
        (tmp_path / "infra.env").chmod(0o644)
    elif kind == "relative-infra":
        path.write_text(path.read_text().replace(str(tmp_path / "infra.env"), "infra.env"))
    else:
        path.write_text(path.read_text().replace(str(tmp_path / "infra.env"), "${PRIVATE_INFRA}"))
    with pytest.raises(ValueError, match="^DEVELOPMENT_CONFIGURATION_BLOCKED$"):
        load_development(path)


def test_live_email_private_recipient_pin_is_backend_only(tmp_path: Path) -> None:
    from aco.core.development import load_development

    path = configuration(tmp_path)
    path.write_text(
        path.read_text().replace("DEV_MODE=offline", "DEV_MODE=live")
        + "EMAIL_ADAPTER=resend\nACO_EMAIL_LIVE_CONFIG_PATH=/run/aco/providers/email.json\n"
        + "ACO_DELIVERY_APPROVED_EMAIL_RECIPIENT=approved@example.test\n"
        + "OUTBOUND_ENABLED=true\nAUTOMATION_KILL_SWITCH=false\n"
    )
    config = load_development(path)
    assert config.backend_environment()["ACO_DELIVERY_APPROVED_EMAIL_RECIPIENT"] == "approved@example.test"
    assert "ACO_DELIVERY_APPROVED_EMAIL_RECIPIENT" not in config.web_environment()
    assert not config.settings().synthetic_email_enabled
    assert config.settings().outbound_enabled and not config.settings().kill_switch


def test_typed_email_selection_cannot_silently_disappear_from_api_composition() -> None:
    from aco.core.application import configured_app
    from aco.core.settings import Settings

    selected = Settings(email_live_config_path=Path("/private/tmp/explicit-typed-email.json"))
    with pytest.raises(RuntimeError, match="^EXPLICIT_PROVIDER_CONFIGURATION_BLOCKED$"):
        configured_app(selected, {})


def test_typed_email_selection_cannot_conflict_with_worker_selection() -> None:
    from aco.core.application import configured_worker
    from aco.core.settings import Settings

    selected = Settings(email_live_config_path=Path("/private/tmp/explicit-typed-email.json"))
    with pytest.raises(ValueError, match="^EMAIL_PRIVATE_CONFIGURATION_REQUIRED$"):
        configured_worker(selected, {"ACO_EMAIL_LIVE_CONFIG_PATH": "/private/tmp/different-email.json"})


def test_existing_local_loader_retains_email_selection_without_synthetic_enablement() -> None:
    from aco.core.local_runtime import local_settings

    settings = local_settings(
        {
            "ACO_EMAIL_LIVE_CONFIG_PATH": "/private/tmp/explicit-email.json",
            "OUTBOUND_ENABLED": "true",
            "AUTOMATION_KILL_SWITCH": "false",
        }
    )
    assert settings.email_live_config_path == Path("/private/tmp/explicit-email.json")
    assert not settings.synthetic_email_enabled


@pytest.mark.parametrize("mutation", ["remote-db", "docker-host", "directory-mode", "selector-traversal"])
def test_docker_configuration_cannot_redirect_runtime_or_escape_private_mount(
    tmp_path: Path, mutation: str
) -> None:
    from aco.core.development import load_development

    path = configuration(tmp_path)
    infra = tmp_path / "infra.env"
    if mutation == "remote-db":
        infra.write_text(infra.read_text().replace("@postgres:5432", "@remote.example.test:5432"))
    elif mutation == "docker-host":
        infra.write_text(infra.read_text() + "DOCKER_HOST=tcp://remote.example.test:2375\n")
    elif mutation == "directory-mode":
        (tmp_path / "providers").chmod(0o755)
    else:
        path.write_text(path.read_text().replace("DEV_MODE=offline", "DEV_MODE=live")
                        + "EMAIL_ADAPTER=resend\n"
                        + "ACO_EMAIL_LIVE_CONFIG_PATH=/run/aco/providers/../escape.json\n")
    with pytest.raises(ValueError, match="^DEVELOPMENT_CONFIGURATION_BLOCKED$"):
        load_development(path)
