"""Real local SQL/OIDC/S3 readiness; no authentication or StoragePort claim."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aco.api.app import create_app
from aco.core.settings import load_settings


def local_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if Path(".env").exists():
        for line in Path(".env").read_text().splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key] = value
    import os

    env.update({key: value for key, value in os.environ.items() if key.startswith("ACO_TEST_")})
    password = env.get("DATABASE_PASSWORD", "")
    return {
        "DATABASE_URL": env.get(
            "ACO_TEST_DATABASE_URL", f"postgresql+psycopg://aco:{password}@127.0.0.1:55432/aco"
        ),
        "STORAGE_ENDPOINT": env.get("ACO_TEST_STORAGE_ENDPOINT", "http://127.0.0.1:59000"),
        "OIDC_DISCOVERY_URL": env.get(
            "ACO_TEST_OIDC_DISCOVERY_URL",
            "http://127.0.0.1:58080/realms/aco/.well-known/openid-configuration",
        ),
        "OIDC_ISSUER": env.get("ACO_TEST_OIDC_ISSUER", "http://127.0.0.1:58080/realms/aco"),
    }


@pytest.mark.integration
def test_integration_allows_only_enumerated_local_services() -> None:
    with TestClient(create_app(load_settings(local_env()))) as client:
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["checks"] == {
            "database": "READY",
            "storage": "READY",
            "identity": "READY",
            "worker": "UNAVAILABLE",
        }
        assert client.get("/health/live").status_code == 200


def test_readiness_fails_for_unreachable_database_and_issuer_mismatch() -> None:
    env = local_env()
    env["DATABASE_URL"] = "postgresql+psycopg://synthetic:synthetic@127.0.0.1:55431/aco"
    env["OIDC_ISSUER"] = "https://synthetic.invalid/wrong-issuer"
    with TestClient(create_app(load_settings(env))) as client:
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["checks"]["database"] == "UNAVAILABLE"
        assert response.json()["checks"]["identity"] == "UNAVAILABLE"
        assert "synthetic:synthetic" not in response.text


@pytest.mark.parametrize("dependency", ["storage", "identity"])
def test_invalid_http_config_does_not_hide_other_real_dependency_checks(dependency: str) -> None:
    env = local_env()
    key = "STORAGE_ENDPOINT" if dependency == "storage" else "OIDC_DISCOVERY_URL"
    env[key] = "http://127.0.0.1:invalid"
    with TestClient(create_app(load_settings(env)), raise_server_exceptions=False) as client:
        response = client.get("/health/ready")
        assert response.status_code == 503
        checks = response.json()["checks"]
        assert checks[dependency] == "UNAVAILABLE"
        assert checks["database"] == "READY"
        assert checks["identity" if dependency == "storage" else "storage"] == "READY"
        assert checks["worker"] == "UNAVAILABLE"
        assert client.get("/health/live").status_code == 200


def test_full_identity_config_preserves_malformed_database_liveness() -> None:
    from cryptography.fernet import Fernet
    from pydantic import SecretStr

    from aco.core.settings import Settings

    settings = Settings(
        database_url=SecretStr("not-a-database-url"),
        identity_issuer=local_env()["OIDC_ISSUER"],
        identity_discovery_url=local_env()["OIDC_DISCOVERY_URL"],
        identity_client_secret=SecretStr("synthetic"),
        session_secret=SecretStr("synthetic-signing-secret-32-chars-long"),
        token_encryption_key=SecretStr(Fernet.generate_key().decode()),
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/health/live").status_code == 200
        ready = client.get("/health/ready")
        assert ready.status_code == 503
        assert ready.json()["checks"]["database"] == "UNAVAILABLE"
        for route in ["/api/auth/login", "/api/auth/callback", "/api/me"]:
            response = client.get(route, headers={"cookie": "aco_session=synthetic"}, follow_redirects=False)
            assert response.status_code == 503
            assert response.json() == {"detail": "TEMPORARILY_UNAVAILABLE"}
            assert "not-a-database-url" not in response.text
