"""Actual Owner setup verifies APP storage before readiness; no READY fixture shortcut."""

import asyncio
from typing import cast
from uuid import uuid4

import pytest
from action_support import action_world
from artifact_support import storage_configuration
from fastapi import FastAPI
from sqlalchemy import func, select

from aco.features.communications.ingress import EmailIngressService
from aco.features.identity.models import ProviderConnection
from aco.features.usage.models import ProviderUsageEvent
from aco.integrations.ports.storage import ArtifactObjectRef, StorageFailure
from aco.integrations.s3.adapter import S3StorageAdapter, StorageRuntime


def test_owner_setup_probes_private_storage_and_reuses_exact_verified_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with action_world() as world:
        app = cast(FastAPI, world.client.app)
        runtime = StorageRuntime(**storage_configuration())
        service = EmailIngressService(
            app.state.business_sessions,
            enabled=True,
            default_secret="whsec_c3ludGhldGljLXNldHVwLXNlY3JldC1vbmx5",
            storage_runtime=runtime,
            environment="local",
        )
        app.state.email_ingress = service
        refs: list[ArtifactObjectRef] = []
        original = S3StorageAdapter.put_private_artifact

        async def observe(self: S3StorageAdapter, ctx: object, command: object) -> object:
            from aco.core.context import TenantContext
            from aco.integrations.ports.storage import ObjectWrite

            assert isinstance(ctx, TenantContext) and isinstance(command, ObjectWrite)
            with app.state.sessions() as db:
                connection = db.get(ProviderConnection, self.connection_id)
                assert connection is not None and connection.status == "NOT_CONFIGURED"
            result = await original(self, ctx, command)
            refs.append(result.value)
            return result

        monkeypatch.setattr(S3StorageAdapter, "put_private_artifact", observe)
        path = f"/api/workspaces/{world.ctx.tenant_id}/email-ingress/setup"
        before = world.client.get(path)
        assert before.status_code == 200 and before.json()["can_prepare"] is True
        assert before.json()["email_connections"] == [] and before.json()["storage_connections"] == []
        command = {"request_id": str(uuid4()), "evidence": "Explicit Owner synthetic inbox preparation."}
        response = world.client.post(path, json=command)
        assert response.status_code == 200, response.text
        assert len(response.json()["email_connections"]) == len(response.json()["storage_connections"]) == 1
        db = world.store
        db.rollback()
        connections = list(
            db.scalars(
                select(ProviderConnection).where(ProviderConnection.provider_type.in_(["email", "storage"]))
            )
        )
        assert len(connections) == 2 and all(row.status == "READY" for row in connections)
        bindings = {(row.id, row.provider, row.version) for row in connections}
        assert (
            db.scalar(
                select(func.count())
                .select_from(ProviderUsageEvent)
                .where(ProviderUsageEvent.operation_key.like("ingress-setup:%"))
            )
            == 5
        )
        assert len(refs) == 1
        with pytest.raises(StorageFailure) as missing:
            asyncio.run(
                runtime.bind(world.ctx.tenant_id, refs[0].connection_id).get_metadata(world.ctx, refs[0])
            )
        assert missing.value.reason == "OBJECT_MISSING"
        db.rollback()
        assert world.client.post(path, json=command).status_code == 200
        assert (
            world.client.post(
                path, json={**command, "evidence": "Changed reviewed preparation evidence."}
            ).status_code
            == 409
        )
        assert world.client.post(path, json={**command, "request_id": str(uuid4())}).status_code == 200
        db.rollback()
        assert {
            (row.id, row.provider, row.version)
            for row in db.scalars(
                select(ProviderConnection).where(ProviderConnection.provider_type.in_(["email", "storage"]))
            )
        } == bindings
        assert (
            db.scalar(
                select(func.count())
                .select_from(ProviderUsageEvent)
                .where(ProviderUsageEvent.operation_key.like("ingress-setup:%"))
            )
            == 5
        )
        service.enabled = False
        assert world.client.get(path).json()["can_prepare"] is False
        assert world.client.post(path, json=command).status_code == 403


def test_failed_integrity_keeps_connections_unconfigured_cleans_object_and_allows_exact_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with action_world() as world:
        app = cast(FastAPI, world.client.app)
        runtime = StorageRuntime(**storage_configuration())
        app.state.email_ingress = EmailIngressService(
            app.state.business_sessions,
            enabled=True,
            default_secret="whsec_c3ludGhldGljLXNldHVwLXNlY3JldC1vbmx5",
            storage_runtime=runtime,
            environment="local",
        )
        refs: list[ArtifactObjectRef] = []
        original = S3StorageAdapter.verify_integrity

        async def fail_integrity(self: S3StorageAdapter, ctx: object, ref: ArtifactObjectRef) -> object:
            from aco.core.context import TenantContext

            assert isinstance(ctx, TenantContext)
            observed = await original(self, ctx, ref)
            assert observed.value is True
            refs.append(ref)
            return observed.model_copy(update={"value": False})

        monkeypatch.setattr(S3StorageAdapter, "verify_integrity", fail_integrity)
        path = f"/api/workspaces/{world.ctx.tenant_id}/email-ingress/setup"
        command = {"request_id": str(uuid4()), "evidence": "Owner requested synthetic inbox probe retry."}
        response = world.client.post(path, json=command)
        assert response.status_code == 503 and response.json()["detail"] == "INGRESS_SETUP_PROBE_FAILED"
        db = world.store
        db.rollback()
        connections = list(
            db.scalars(
                select(ProviderConnection).where(ProviderConnection.provider_type.in_(["email", "storage"]))
            )
        )
        assert len(connections) == 2 and all(row.status == "NOT_CONFIGURED" for row in connections)
        ids = {row.id for row in connections}
        assert len(refs) == 1
        with pytest.raises(StorageFailure) as missing:
            asyncio.run(
                runtime.bind(world.ctx.tenant_id, refs[0].connection_id).get_metadata(world.ctx, refs[0])
            )
        assert missing.value.reason == "OBJECT_MISSING"
        assert (
            db.scalar(
                select(func.count())
                .select_from(ProviderUsageEvent)
                .where(ProviderUsageEvent.operation_key.like("ingress-setup:%"))
            )
            == 3
        )
        db.rollback()
        monkeypatch.setattr(S3StorageAdapter, "verify_integrity", original)
        assert world.client.post(path, json=command).status_code == 200
        db.rollback()
        db.expire_all()
        connections = list(
            db.scalars(
                select(ProviderConnection).where(ProviderConnection.provider_type.in_(["email", "storage"]))
            )
        )
        assert {row.id for row in connections} == ids
        assert all(row.status == "READY" for row in connections)
        assert (
            db.scalar(
                select(func.count())
                .select_from(ProviderUsageEvent)
                .where(ProviderUsageEvent.operation_key.like("ingress-setup:%"))
            )
            == 8
        )
