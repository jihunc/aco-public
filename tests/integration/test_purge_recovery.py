"""Restarted maintained purge execution preserves failure history and retries the same object."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from artifact_support import artifact_world, storage_configuration, upload
from pydantic import SecretStr
from sqlalchemy import select

from aco.core.settings import Settings
from aco.core.types import ProviderErrorCode, ProviderFailure
from aco.features.artifacts.models import ArtifactPurge
from aco.features.artifacts.retention import dispatch_purges, recover_purges
from aco.features.identity.models import now
from aco.integrations.ports.storage import (
    ArtifactObjectRef,
    StorageFailure,
    StorageInvocationContext,
    StorageResult,
)
from aco.integrations.s3.adapter import S3StorageAdapter
from aco.worker.app import WorkerRuntime


async def test_restarted_purge_retries_due_failure_without_reopening_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with artifact_world() as (client, store, tenant):
        saved = upload(client, tenant, b"Synthetic maintained purge recovery original").json()
        path = f"/api/workspaces/{tenant}/artifacts/{saved['id']}"
        view = client.get(path + "/privacy").json()
        request = client.post(
            path + "/privacy",
            json={
                "request_id": str(uuid4()),
                "expected_version": view["version"],
                "kind": "DELETE_REQUEST",
                "reason": "Synthetic explicit deletion recovery request",
            },
        ).json()
        approved = client.post(
            path + "/privacy",
            json={
                "request_id": str(uuid4()),
                "expected_version": request["version"],
                "kind": "DELETE_APPROVED",
                "parent_id": request["events"][0]["id"],
                "reason": "Current Owner approved exact synthetic deletion",
            },
        )
        assert approved.status_code == 200, approved.text
        config = storage_configuration()
        settings = Settings(
            database_url=SecretStr(store.get_bind().engine.url.render_as_string(hide_password=False)),
            storage_endpoint=config["endpoint"],
            storage_bucket=config["bucket"],
            storage_access_key=config["access_key"],
            storage_secret_key=config["secret_key"],
            storage_local_endpoint_ip=config.get("local_endpoint_ip"),
        )
        original = S3StorageAdapter.delete_object
        calls = 0

        async def unavailable(
            self: S3StorageAdapter, ctx: StorageInvocationContext, ref: ArtifactObjectRef
        ) -> StorageResult[bool]:
            nonlocal calls
            calls += 1
            raise StorageFailure(
                ProviderFailure(
                    code=ProviderErrorCode.TEMPORARILY_UNAVAILABLE, retryable=True, may_have_side_effect=False
                )
            )

        monkeypatch.setattr(S3StorageAdapter, "delete_object", unavailable)
        store.rollback()
        first = WorkerRuntime(settings)
        try:
            async with first.app.open_async():
                assert await dispatch_purges(first.sessions, first.app) == 1
                queued = store.scalar(
                    select(ArtifactPurge).where(ArtifactPurge.artifact_id == UUID(saved["id"]))
                )
                assert queued and queued.job_id is not None
                first_job = queued.job_id
                store.rollback()
                await first.app.run_worker_async(
                    queues=["aco"], concurrency=1, wait=False, listen_notify=False
                )
        finally:
            await first.engine.dispose()
        store.expire_all()
        purge = store.scalar(select(ArtifactPurge).where(ArtifactPurge.artifact_id == UUID(saved["id"])))
        assert purge and purge.status in {"blocked", "purge_pending"} and purge.confirmed_at is None
        assert purge.failure_code == "TEMPORARILY_UNAVAILABLE" and calls == 1
        recovery_pending = purge.job_id is not None
        assert client.get(path + "/content").status_code == 404
        # Advance only the scheduler's clock past the persisted retry deadline; no source/authority edits.
        due = purge.due_at + timedelta(seconds=1)
        monkeypatch.setattr("aco.features.artifacts.retention.now", lambda: max(now(), due))
        monkeypatch.setattr(S3StorageAdapter, "delete_object", original)
        store.rollback()
        restarted = WorkerRuntime(settings)
        try:
            async with restarted.app.open_async():
                assert await recover_purges(restarted.sessions) == int(recovery_pending)
                assert await dispatch_purges(restarted.sessions, restarted.app) == 1
                await restarted.app.run_worker_async(
                    queues=["aco"], concurrency=1, wait=False, listen_notify=False
                )
        finally:
            await restarted.engine.dispose()
        store.expire_all()
        current = store.get(ArtifactPurge, purge.id)
        assert current and current.status == "deleted" and current.provider_receipt and current.confirmed_at
        assert current.job_version == 2 and current.job_id != first_job
        assert client.get(path + "/content").status_code == 404


def test_purge_job_reference_matches_maintained_bigint_schema() -> None:
    from sqlalchemy import BigInteger, inspect

    with artifact_world() as (_, store, _):
        actual = next(
            column
            for column in inspect(store.connection()).get_columns("artifact_purge_requests")
            if column["name"] == "job_id"
        )
        assert isinstance(actual["type"], BigInteger)
        assert isinstance(ArtifactPurge.__table__.c.job_id.type, BigInteger)
