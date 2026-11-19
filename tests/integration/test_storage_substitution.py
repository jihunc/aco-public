"""The real consumer accepts a separately named provider without vendor branching."""

import hashlib
from dataclasses import replace
from typing import cast
from uuid import UUID

import pytest
from artifact_support import upload
from fastapi import FastAPI
from identity_support import actor_client
from sqlalchemy import select

from aco.core.context import ActorContext
from aco.core.types import CostQuality, ProviderCapabilities, ProviderCost
from aco.features.identity.models import ProviderConnection
from aco.features.usage.models import ProviderUsageEvent
from aco.integrations.ports.storage import ArtifactObjectRef, ObjectWrite, StorageResult


class MemoryStorage:
    provider = "synthetic-memory"

    def __init__(self) -> None:
        self.objects: dict[str, tuple[ArtifactObjectRef, bytes]] = {}

    def bind(self, tenant_id: UUID, connection_id: UUID) -> "MemoryPort":
        return MemoryPort(self, tenant_id, connection_id)


class MemoryPort:
    provider = "synthetic-memory"

    def __init__(self, runtime: MemoryStorage, tenant_id: UUID, connection_id: UUID) -> None:
        self.runtime, self.tenant_id, self.connection_id = runtime, tenant_id, connection_id

    def result[T](self, ctx: ActorContext, value: T) -> StorageResult[T]:
        assert ctx.tenant_id == self.tenant_id
        return StorageResult(
            value=value,
            tenant_id=self.tenant_id,
            connection_id=self.connection_id,
            provider=self.provider,
            correlation_id=ctx.correlation_id,
            native_operation_id=None,
            cost=ProviderCost(amount=None, currency="USD", quality=CostQuality.UNKNOWN),
        )

    async def put_private_artifact(
        self, ctx: ActorContext, command: ObjectWrite
    ) -> StorageResult[ArtifactObjectRef]:
        assert hashlib.sha256(command.body).hexdigest() == command.sha256
        key = f"tenants/{self.tenant_id}/artifacts/{command.artifact_id}/{command.sha256}"
        ref = ArtifactObjectRef(
            tenant_id=self.tenant_id,
            connection_id=self.connection_id,
            artifact_id=command.artifact_id,
            key=key,
            sha256=command.sha256,
            content_type=command.content_type,
            size_bytes=len(command.body),
            version=None,
            encryption="synthetic-memory-only",
        )
        self.runtime.objects[key] = (ref, command.body)
        return self.result(ctx, ref)

    async def read_private_artifact(self, ctx: ActorContext, ref: ArtifactObjectRef) -> StorageResult[bytes]:
        return self.result(ctx, self.runtime.objects[ref.key][1])

    async def get_metadata(
        self, ctx: ActorContext, ref: ArtifactObjectRef
    ) -> StorageResult[ArtifactObjectRef]:
        return self.result(ctx, self.runtime.objects[ref.key][0])

    async def verify_integrity(self, ctx: ActorContext, ref: ArtifactObjectRef) -> StorageResult[bool]:
        return self.result(ctx, hashlib.sha256(self.runtime.objects[ref.key][1]).hexdigest() == ref.sha256)

    async def delete_object(self, ctx: ActorContext, ref: ArtifactObjectRef) -> StorageResult[bool]:
        self.runtime.objects.pop(ref.key, None)
        return self.result(ctx, True)

    async def get_signed_read_url(
        self, ctx: ActorContext, ref: ArtifactObjectRef, ttl_seconds: int
    ) -> StorageResult[str]:
        raise NotImplementedError("Synthetic memory provider does not support signed URLs")

    async def describe_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            features=frozenset({"authenticated_binary_read", "integrity", "provider_purge"})
        )


def test_real_consumer_preserves_replacement_provider_provenance() -> None:
    with actor_client("owner") as (client, store, _, tenant):
        runtime = MemoryStorage()
        store.add(
            ProviderConnection(
                tenant_id=tenant,
                provider=runtime.provider,
                provider_type="storage",
                status="READY",
                external_account_id="synthetic-only",
            )
        )
        store.commit()
        app = cast(FastAPI, client.app)
        app.state.ports = replace(app.state.ports, storage=runtime)
        response = upload(client, tenant, b"Synthetic replacement provider")
        assert response.status_code == 201, response.text
        path = f"/api/workspaces/{tenant}/artifacts/{response.json()['id']}"
        assert client.get(path + "/content").content == b"Synthetic replacement provider"
        assert (
            client.request("DELETE", path, json={"reason": "synthetic test cleanup"}).json()["status"]
            == "deleted"
        )
        assert not runtime.objects
        events = list(
            store.scalars(select(ProviderUsageEvent).where(ProviderUsageEvent.provider_kind == "storage"))
        )
        assert len(events) == 3 and all(event.provider == runtime.provider for event in events)


def test_mismatched_bound_provider_is_rejected_before_artifact_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with actor_client("owner") as (client, store, _, tenant):
        runtime = MemoryStorage()
        store.add(
            ProviderConnection(
                tenant_id=tenant,
                provider=runtime.provider,
                provider_type="storage",
                status="READY",
                external_account_id="synthetic-only",
            )
        )
        store.commit()
        original = runtime.bind

        def wrong_provider(tenant_id: UUID, connection_id: UUID) -> MemoryPort:
            port = original(tenant_id, connection_id)
            port.provider = "wrong-provider"
            return port

        monkeypatch.setattr(runtime, "bind", wrong_provider)
        app = cast(FastAPI, client.app)
        app.state.ports = replace(app.state.ports, storage=runtime)
        response = upload(client, tenant, b"Synthetic mismatched binding")
        assert not runtime.objects
        assert response.status_code == 503


def test_unconfirmed_provider_delete_never_becomes_success_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    async def not_deleted(self: MemoryPort, ctx: ActorContext, ref: ArtifactObjectRef) -> StorageResult[bool]:
        return self.result(ctx, False)

    monkeypatch.setattr(MemoryPort, "delete_object", not_deleted)
    with actor_client("owner") as (client, store, _, tenant):
        runtime = MemoryStorage()
        store.add(
            ProviderConnection(
                tenant_id=tenant,
                provider=runtime.provider,
                provider_type="storage",
                status="READY",
                external_account_id="synthetic-only",
            )
        )
        store.commit()
        app = cast(FastAPI, client.app)
        app.state.ports = replace(app.state.ports, storage=runtime)
        artifact = upload(client, tenant, b"Synthetic unconfirmed deletion").json()
        response = client.request(
            "DELETE", f"/api/workspaces/{tenant}/artifacts/{artifact['id']}", json={"reason": "request"}
        )
        assert response.json()["status"] == "blocked"
        assert response.json()["confirmed_at"] is None
        assert runtime.objects
