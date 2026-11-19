"""Regression from C independent review: legal review cannot starve lawful deletion."""

from uuid import UUID, uuid4

from artifact_support import artifact_world, storage_configuration, upload
from pydantic import SecretStr
from sqlalchemy import select

from aco.core.settings import Settings
from aco.features.artifacts.models import ArtifactPurge
from aco.features.artifacts.retention import dispatch_purges
from aco.worker.app import WorkerRuntime


async def test_blocked_due_prefix_does_not_starve_an_authorized_purge() -> None:
    with artifact_world() as (client, store, tenant):
        target: UUID | None = None
        for index in range(21):
            saved = upload(client, tenant, f"Synthetic private deletion fairness {index}".encode()).json()
            prefix = f"/api/workspaces/{tenant}/artifacts/{saved['id']}"
            view = client.get(prefix + "/privacy").json()
            if index < 20:
                held = client.post(
                    prefix + "/privacy",
                    json={
                        "request_id": str(uuid4()),
                        "expected_version": view["version"],
                        "kind": "QUARANTINE",
                        "reason": "Synthetic unresolved legal review fixture",
                    },
                )
                assert held.status_code == 200, held.text
                assert client.get(prefix + "/content").status_code == 404
            else:
                requested = client.post(
                    prefix + "/privacy",
                    json={
                        "request_id": str(uuid4()),
                        "expected_version": view["version"],
                        "kind": "DELETE_REQUEST",
                        "reason": "Synthetic authorized private deletion fixture",
                    },
                )
                assert requested.status_code == 200, requested.text
                body = requested.json()
                approved = client.post(
                    prefix + "/privacy",
                    json={
                        "request_id": str(uuid4()),
                        "expected_version": body["version"],
                        "parent_id": body["events"][0]["id"],
                        "kind": "DELETE_APPROVED",
                        "reason": "Current Owner approved this exact synthetic deletion",
                    },
                )
                assert approved.status_code == 200, approved.text
                target = UUID(saved["id"])
        assert target is not None
        config = storage_configuration()
        url = store.get_bind().engine.url.render_as_string(hide_password=False)
        store.rollback()
        worker = WorkerRuntime(
            Settings(
                database_url=SecretStr(url),
                storage_endpoint=config["endpoint"],
                storage_bucket=config["bucket"],
                storage_access_key=config["access_key"],
                storage_secret_key=config["secret_key"],
                storage_local_endpoint_ip=config.get("local_endpoint_ip"),
            )
        )
        try:
            async with worker.app.open_async():
                dispatched = sum([await dispatch_purges(worker.sessions, worker.app) for _ in range(3)])
            store.expire_all()
            request = store.scalar(select(ArtifactPurge).where(ArtifactPurge.artifact_id == target))
            assert request is not None
            assert dispatched == 1 and request.job_id is not None, (dispatched, request.status)
        finally:
            await worker.engine.dispose()
