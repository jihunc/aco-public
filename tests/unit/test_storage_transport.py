"""Trusted local topology is exact, explicit, and never a public HTTP allowance."""

from pathlib import Path
from uuid import uuid4

import pytest
from artifact_support import StorageCredentials
from pydantic import SecretStr

from aco.integrations.s3.adapter import S3StorageAdapter, StorageRuntime


def configuration() -> StorageCredentials:
    return dict(
        bucket="synthetic-artifacts",
        access_key=SecretStr("synthetic-app-access"),
        secret_key=SecretStr("synthetic-app-secret"),
    )


def test_runtime_forwards_exact_trusted_container_endpoint() -> None:
    runtime = StorageRuntime(
        endpoint="http://172.18.0.4:8333", local_endpoint_ip="172.18.0.4", **configuration()
    )
    adapter = runtime.bind(uuid4(), uuid4())
    assert adapter.client.meta.endpoint_url == "http://172.18.0.4:8333"


@pytest.mark.parametrize(
    "endpoint,trusted",
    [
        ("http://8.8.8.8:8333", "8.8.8.8"),
        ("http://172.18.0.5:8333", "172.18.0.4"),
        ("http://172.18.0.4:9000", "172.18.0.4"),
        ("http://127.0.0.1:8333", "127.0.0.1"),
        ("http://169.254.1.2:8333", "169.254.1.2"),
        ("http://224.0.0.1:8333", "224.0.0.1"),
        ("http://0.0.0.0:8333", "0.0.0.0"),
        ("http://[fd00::1]:8333", "fd00::1"),
    ],
)
def test_invalid_or_mismatched_local_exception_is_rejected(endpoint: str, trusted: str) -> None:
    with pytest.raises(ValueError):
        S3StorageAdapter(
            tenant_id=uuid4(),
            connection_id=uuid4(),
            endpoint=endpoint,
            local_endpoint_ip=trusted,
            **configuration(),
        )


def test_untrusted_private_http_is_still_rejected_and_https_remains_allowed() -> None:
    with pytest.raises(ValueError):
        S3StorageAdapter(
            tenant_id=uuid4(), connection_id=uuid4(), endpoint="http://172.18.0.4:8333", **configuration()
        )
    adapter = S3StorageAdapter(
        tenant_id=uuid4(), connection_id=uuid4(), endpoint="https://objects.example.test", **configuration()
    )
    assert adapter.client.meta.endpoint_url == "https://objects.example.test"


def test_explicit_portable_fixture_configuration_overrides_native_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from artifact_support import storage_configuration

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "STORAGE_APP_ACCESS_KEY=native-key\nSTORAGE_APP_SECRET_KEY=native-secret\n"
    )
    values = {
        "ACO_TEST_STORAGE_ENDPOINT": "http://172.18.0.4:8333",
        "ACO_TEST_STORAGE_BUCKET": "synthetic-portable",
        "ACO_TEST_STORAGE_APP_ACCESS_KEY": "portable-app-key",
        "ACO_TEST_STORAGE_APP_SECRET_KEY": "portable-app-secret",
        "ACO_LOCAL_S3_IP": "172.18.0.4",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    result = storage_configuration()
    assert result["endpoint"] == values["ACO_TEST_STORAGE_ENDPOINT"]
    assert result["bucket"] == values["ACO_TEST_STORAGE_BUCKET"]
    assert result["access_key"].get_secret_value() == values["ACO_TEST_STORAGE_APP_ACCESS_KEY"]
    assert result["secret_key"].get_secret_value() == values["ACO_TEST_STORAGE_APP_SECRET_KEY"]
    assert result["local_endpoint_ip"] == values["ACO_LOCAL_S3_IP"]
    monkeypatch.delenv("ACO_TEST_STORAGE_APP_SECRET_KEY")
    with pytest.raises(ValueError, match="Incomplete explicit storage"):
        storage_configuration()
