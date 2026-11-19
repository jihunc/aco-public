"""Catch readiness claimed before real dependencies or worker recovery are present."""

import pytest
from fastapi.testclient import TestClient

from aco.api.app import create_app
from aco.core.settings import load_settings


def test_live_and_ready_are_distinct_and_no_worker_is_fabricated() -> None:
    with TestClient(create_app(load_settings({}))) as client:
        assert client.get("/health/live").json() == {"status": "alive"}
        response = client.get("/health/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "not_ready"
        assert body["checks"] == {
            "database": "NOT_CONFIGURED",
            "storage": "NOT_CONFIGURED",
            "identity": "NOT_CONFIGURED",
            "worker": "NOT_CONFIGURED",
        }
        assert client.get("/openapi.json").json()["paths"]["/health/ready"]["get"]["responses"].get("503")


def test_malformed_database_configuration_is_unavailable_and_redacted() -> None:
    with TestClient(create_app(load_settings({"DATABASE_URL": "synthetic-secret-invalid-dsn"}))) as client:
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["checks"]["database"] == "UNAVAILABLE"
        assert "synthetic-secret" not in response.text


@pytest.mark.parametrize("url", ["http://127.0.0.1:invalid", "http://\x00"])
@pytest.mark.parametrize("dependency", ["storage", "identity"])
def test_malformed_http_dependency_returns_unavailable_with_live_available(url: str, dependency: str) -> None:
    env = (
        {"STORAGE_ENDPOINT": url}
        if dependency == "storage"
        else {"OIDC_DISCOVERY_URL": url, "OIDC_ISSUER": "synthetic-secret-issuer"}
    )
    with TestClient(create_app(load_settings(env)), raise_server_exceptions=False) as client:
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["checks"][dependency] == "UNAVAILABLE"
        assert "synthetic-secret" not in response.text and url not in response.text
        assert client.get("/health/live").json() == {"status": "alive"}
