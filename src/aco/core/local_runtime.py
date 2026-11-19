"""Explicit shared API/worker local configuration; never forward the complete environment."""

import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import URL

from aco.core.settings import Settings, load_settings


def local_settings(values: Mapping[str, str | None] | None = None) -> Settings:
    source = values if values is not None else {**dotenv_values(Path(".env")), **os.environ}
    database_url = URL.create(
        "postgresql+psycopg",
        username="aco",
        password=source.get("DATABASE_PASSWORD"),
        host="127.0.0.1",
        port=55432,
        database="aco",
    ).render_as_string(hide_password=False)
    return load_settings(
        {
            "APP_ENV": source.get("APP_ENV") or "local",
            "ACO_VOICE_LIVE_CONFIG_PATH": source.get("ACO_VOICE_LIVE_CONFIG_PATH") or "",
            "ACO_EMAIL_LIVE_CONFIG_PATH": source.get("ACO_EMAIL_LIVE_CONFIG_PATH") or "",
            "SYNTHETIC_VOICE_ENABLED": source.get("SYNTHETIC_VOICE_ENABLED") or "false",
            "OIDC_CLIENT_SECRET": source.get("ACO_TEST_OIDC_CLIENT_SECRET")
            or source.get("OIDC_CLIENT_SECRET")
            or "",
            "SESSION_SECRET": source.get("ACO_TEST_SESSION_SECRET") or source.get("SESSION_SECRET") or "",
            "TOKEN_ENCRYPTION_KEY": source.get("ACO_TEST_TOKEN_ENCRYPTION_KEY")
            or source.get("TOKEN_ENCRYPTION_KEY")
            or "",
            "WEB_PUBLIC_URL": "http://127.0.0.1:53000",
            "DATABASE_URL": source.get("ACO_TEST_DATABASE_URL") or database_url,
            "STORAGE_ENDPOINT": source.get("ACO_TEST_STORAGE_ENDPOINT")
            or source.get("STORAGE_ENDPOINT")
            or "http://127.0.0.1:59000",
            "STORAGE_BUCKET": source.get("ACO_TEST_STORAGE_BUCKET")
            or source.get("STORAGE_BUCKET")
            or "aco-artifacts",
            "ACO_LOCAL_S3_IP": source.get("ACO_LOCAL_S3_IP") or "",
            "STORAGE_APP_ACCESS_KEY": source.get("ACO_TEST_STORAGE_APP_ACCESS_KEY")
            or source.get("STORAGE_APP_ACCESS_KEY")
            or "",
            "STORAGE_APP_SECRET_KEY": source.get("ACO_TEST_STORAGE_APP_SECRET_KEY")
            or source.get("STORAGE_APP_SECRET_KEY")
            or "",
            "SYNTHETIC_EMAIL_ENABLED": source.get("SYNTHETIC_EMAIL_ENABLED") or "false",
            "SYNTHETIC_EMAIL_INGRESS_ENABLED": source.get("SYNTHETIC_EMAIL_INGRESS_ENABLED") or "false",
            "SYNTHETIC_EMAIL_INGRESS_WEBHOOK_SECRET": source.get("SYNTHETIC_EMAIL_INGRESS_WEBHOOK_SECRET")
            or "",
            "OUTBOUND_ENABLED": source.get("OUTBOUND_ENABLED") or "false",
            "AUTOMATION_KILL_SWITCH": source.get("AUTOMATION_KILL_SWITCH") or "true",
            "OIDC_DISCOVERY_URL": source.get("ACO_TEST_OIDC_DISCOVERY_URL")
            or "http://127.0.0.1:58080/realms/aco/.well-known/openid-configuration",
            "OIDC_ISSUER": source.get("ACO_TEST_OIDC_ISSUER") or "http://127.0.0.1:58080/realms/aco",
        }
    )


def local_email_config_path(values: Mapping[str, str | None] | None = None) -> Path | None:
    """Only explicit private operator selection; never inspect the live credential file."""
    source = values if values is not None else {**dotenv_values(Path(".env")), **os.environ}
    selected = source.get("ACO_EMAIL_LIVE_CONFIG_PATH")
    # Preserve the original path for the loader's absolute/no-symlink/0600 checks.
    return Path(selected) if selected else None


def local_provider_selection(values: Mapping[str, str | None] | None = None) -> dict[str, str | None]:
    """Local launchers forward only explicit provider selectors, never raw credentials."""
    source = values if values is not None else {**dotenv_values(Path(".env")), **os.environ}
    return {
        key: source.get(key)
        for key in (
            "ACO_AGENT_LIVE_CONFIG_PATH",
            "ACO_AGENT_LIVE_GRANT_ID",
            "ACO_LIVE_AUTHORIZATION_REF",
            "ACO_LIVE_CAMPAIGN_FILE",
            "ACO_LIVE_ALLOCATION_ID",
            "ACO_EMAIL_LIVE_CONFIG_PATH",
            "ACO_VOICE_LIVE_CONFIG_PATH",
        )
    }
