"""Local API/worker settings select only explicitly approved application values."""

import importlib.util
from pathlib import Path
from typing import Any

import pytest


def loader() -> Any:
    spec = importlib.util.spec_from_file_location("aco_local_runtime", Path("scripts/local_runtime.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.local_settings


def test_local_runtime_default_off_and_app_only_explicit_synthetic_activation() -> None:
    load = loader()
    settings = load({})
    assert not settings.synthetic_email_enabled and not settings.outbound_enabled and settings.kill_switch
    reviewed = {
        "SYNTHETIC_EMAIL_ENABLED": "true",
        "OUTBOUND_ENABLED": "true",
        "AUTOMATION_KILL_SWITCH": "false",
        "STORAGE_APP_ACCESS_KEY": "synthetic-app",
        "STORAGE_APP_SECRET_KEY": "synthetic-secret",
        "STORAGE_ADMIN_ACCESS_KEY": "ignored-admin",
        "EXTERNAL_PROVIDERS": "live",
    }
    api, worker = load(reviewed), load(reviewed)
    assert api == worker
    assert api.synthetic_email_enabled and api.outbound_enabled and not api.kill_switch
    assert api.external_providers == "fake"
    assert api.storage_access_key.get_secret_value() == "synthetic-app"
    assert api.storage_secret_key.get_secret_value() == "synthetic-secret"
    with pytest.raises(ValueError):
        load({"OUTBOUND_ENABLED": "true"})


def test_api_and_worker_bind_storage_at_exact_verified_container_address() -> None:
    from uuid import uuid4

    from aco.core.settings import resolve_ports
    from aco.integrations.s3.adapter import StorageRuntime

    settings = loader()(
        {
            "ACO_TEST_STORAGE_ENDPOINT": "http://172.18.0.2:8333",
            "ACO_LOCAL_S3_IP": "172.18.0.2",
            "STORAGE_APP_ACCESS_KEY": "synthetic-app",
            "STORAGE_APP_SECRET_KEY": "synthetic-secret",
        }
    )
    runtime = resolve_ports(settings).storage
    assert isinstance(runtime, StorageRuntime)
    port = runtime.bind(uuid4(), uuid4())
    assert port.client.meta.endpoint_url == "http://172.18.0.2:8333"


@pytest.mark.parametrize("trusted", [None, "172.18.0.3", "8.8.8.8", "169.254.1.1"])
def test_unverified_container_storage_addresses_remain_rejected(trusted: Any) -> None:
    from uuid import uuid4

    from aco.core.settings import resolve_ports

    values = {
        "ACO_TEST_STORAGE_ENDPOINT": "http://172.18.0.2:8333",
        "STORAGE_APP_ACCESS_KEY": "synthetic-app",
        "STORAGE_APP_SECRET_KEY": "synthetic-secret",
    }
    if trusted:
        values["ACO_LOCAL_S3_IP"] = trusted
    with pytest.raises(ValueError):
        from aco.integrations.s3.adapter import StorageRuntime

        runtime = resolve_ports(loader()(values)).storage
        assert isinstance(runtime, StorageRuntime)
        runtime.bind(uuid4(), uuid4())
