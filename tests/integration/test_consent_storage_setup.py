"""Actual isolated APP storage readiness; no email activation or forged READY connection."""

from dataclasses import replace
from uuid import uuid4

import pytest
from artifact_support import storage_configuration
from fastapi import FastAPI
from identity_support import actor_client
from pydantic import SecretStr
from sqlalchemy import func, select

from aco.features.identity.models import ProviderConnection
from aco.features.voice.demo import OfflineVoiceDemo
from aco.integrations.ports.storage import ArtifactObjectRef, StorageInvocationContext, StorageResult
from aco.integrations.s3.adapter import S3StorageAdapter, StorageRuntime


@pytest.mark.parametrize("boundary", ["disabled", "viewer", "bad_readback"])
def test_private_storage_preparation_fails_closed(boundary: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with actor_client("viewer" if boundary == "viewer" else "owner") as (client, store, _actor, tenant):
        app = client.app
        assert isinstance(app, FastAPI)

        async def no_callback(raw: bytes, headers: dict[str, str]) -> None:
            raise AssertionError("Storage setup cannot start a call")

        demo = OfflineVoiceDemo(
            app.state.business_sessions,
            capability_secret=SecretStr("fixed-synthetic-storage-test"),
            callback_sender=no_callback,
            enabled=boundary != "disabled",
        )
        app.state.voice_runtime = demo.runtime
        app.state.ports = replace(app.state.ports, storage=StorageRuntime(**storage_configuration()))
        original = S3StorageAdapter.read_private_artifact

        async def wrong(
            self: S3StorageAdapter, ctx: StorageInvocationContext, ref: ArtifactObjectRef
        ) -> StorageResult[bytes]:
            result = await original(self, ctx, ref)
            return result.model_copy(update={"value": b"wrong synthetic probe readback"})

        if boundary == "bad_readback":
            monkeypatch.setattr(S3StorageAdapter, "read_private_artifact", wrong)
        response = client.post(
            f"/api/workspaces/{tenant}/voice-consents/storage/setup",
            json={
                "request_id": str(uuid4()),
                "evidence": "Explicit current Owner private synthetic storage preparation",
            },
        )
        assert response.status_code == (503 if boundary == "bad_readback" else 403), response.text
        store.expire_all()
        assert (
            store.scalar(
                select(func.count())
                .select_from(ProviderConnection)
                .where(ProviderConnection.provider_type == "storage", ProviderConnection.status == "READY")
            )
            == 0
        )
        assert (
            store.scalar(
                select(func.count())
                .select_from(ProviderConnection)
                .where(ProviderConnection.provider_type == "email")
            )
            == 0
        )
