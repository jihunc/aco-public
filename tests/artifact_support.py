"""Isolated synthetic application fixture; real local S3 through production adapter."""

import base64
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import NotRequired, TypedDict, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from dotenv import dotenv_values
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from identity_support import actor_client
from pydantic import SecretStr
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.integrations.s3.adapter import S3StorageAdapter, StorageRuntime


class StorageCredentials(TypedDict):
    bucket: str
    access_key: SecretStr
    secret_key: SecretStr


class StorageConfiguration(StorageCredentials):
    endpoint: str
    local_endpoint_ip: NotRequired[str]


def storage_configuration() -> StorageConfiguration:
    names = {
        "endpoint": "ACO_TEST_STORAGE_ENDPOINT",
        "bucket": "ACO_TEST_STORAGE_BUCKET",
        "access_key": "ACO_TEST_STORAGE_APP_ACCESS_KEY",
        "secret_key": "ACO_TEST_STORAGE_APP_SECRET_KEY",
    }
    if any(name in os.environ for name in names.values()):
        values: dict[str, str] = {}
        for field, name in names.items():
            value = os.environ.get(name)
            if not value:
                raise ValueError("Incomplete explicit storage test configuration")
            values[field] = value
        configuration: StorageConfiguration = dict(
            endpoint=values["endpoint"],
            bucket=values["bucket"],
            access_key=SecretStr(values["access_key"]),
            secret_key=SecretStr(values["secret_key"]),
        )
        endpoint = urlsplit(values["endpoint"])
        trusted = os.environ.get("ACO_LOCAL_S3_IP")
        if trusted and endpoint.scheme == "http" and endpoint.hostname == trusted and endpoint.port == 8333:
            configuration["local_endpoint_ip"] = trusted
        return configuration
    native = dotenv_values(".env")
    access_key, secret_key = native.get("STORAGE_APP_ACCESS_KEY"), native.get("STORAGE_APP_SECRET_KEY")
    if not access_key or not secret_key:
        raise ValueError("Managed application storage credentials required")
    return dict(
        endpoint=native.get("STORAGE_ENDPOINT") or "http://127.0.0.1:59000",
        bucket=native.get("STORAGE_BUCKET") or "aco-artifacts",
        access_key=SecretStr(access_key),
        secret_key=SecretStr(secret_key),
    )


def storage_adapter(
    tenant: UUID | None = None, connection: UUID | None = None
) -> tuple[S3StorageAdapter, TenantContext]:
    from aco.core.types import Role

    tenant = tenant or uuid4()
    adapter = S3StorageAdapter(
        tenant_id=tenant, connection_id=connection or uuid4(), **storage_configuration()
    )
    return adapter, TenantContext(tenant_id=tenant, actor_id=uuid4(), role=Role.OWNER, correlation_id=uuid4())


@contextmanager
def artifact_world(role: str = "owner") -> Iterator[tuple[TestClient, Session, UUID]]:
    from aco.features.identity.models import ProviderConnection

    with actor_client(role) as (client, store, actor, tenant):
        connection = ProviderConnection(
            tenant_id=tenant,
            provider="s3",
            provider_type="storage",
            status="READY",
            external_account_id="aco-artifacts",
        )
        store.add(connection)
        store.commit()
        runtime = StorageRuntime(**storage_configuration())
        app = cast(FastAPI, client.app)
        app.state.ports = replace(app.state.ports, storage=runtime)
        yield client, store, tenant


def upload(
    client: TestClient, tenant: UUID, body: bytes, content_type: str = "text/plain", source: str | None = None
) -> Response:
    return client.post(
        f"/api/workspaces/{tenant}/artifacts",
        json={
            "source_reference": source or str(uuid4()),
            "source_kind": "synthetic_fixture",
            "content_type": content_type,
            "content_base64": base64.b64encode(body).decode(),
            "capture_class": "synthetic_email",
        },
    )


def storage_runtime(client: TestClient) -> StorageRuntime:
    """Narrow the concrete S3 runtime installed by artifact_world."""
    runtime = cast(FastAPI, client.app).state.ports.storage
    assert isinstance(runtime, StorageRuntime)
    return runtime
