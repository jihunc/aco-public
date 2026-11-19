"""Actual offline callbacks and private S3 originals; no microphone or real voice provider."""

from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from action_support import action_world
from artifact_support import storage_configuration
from fastapi import FastAPI
from identity_support import add_actor
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aco.core.types import Role
from aco.features.accounting.models import InvoiceSnapshot
from aco.features.artifacts.models import Artifact, ArtifactPurge
from aco.features.communications.models import Communication, CommunicationInvoiceLink
from aco.features.identity.models import Membership, ProviderConnection, now
from aco.features.review.approval_dto import ApprovalDecisionCommand
from aco.features.review.approvals import HumanApprovalService
from aco.features.review.models import CollectionTask
from aco.features.voice.demo import OfflineVoiceDemo
from aco.integrations.s3.adapter import StorageRuntime


@pytest.mark.parametrize("purge_path", ["human", "worker", "worker_read_race", "worker_retained_pending"])
async def test_permitted_private_originals_history_replay_and_withdrawal(purge_path: str) -> None:
    with action_world() as world:
        app = world.client.app
        assert isinstance(app, FastAPI)
        engine = create_async_engine(world.url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async def deliver(raw: bytes, headers: dict[str, str]) -> None:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post("/api/voice/callback", content=raw, headers=headers)
                assert response.status_code == 200, response.text

        demo = OfflineVoiceDemo(
            sessions,
            capability_secret=SecretStr("synthetic-consent-capture"),
            callback_sender=deliver,
            enabled=True,
        )
        app.state.voice_runtime = demo.runtime
        app.state.ports = replace(app.state.ports, storage=StorageRuntime(**storage_configuration()))
        base = f"/api/workspaces/{world.ctx.tenant_id}"
        storage_command = {
            "request_id": str(uuid4()),
            "evidence": "Explicit Owner prepares private storage for synthetic voice fixtures",
        }
        prepared_storage = world.client.post(base + "/voice-consents/storage/setup", json=storage_command)
        assert prepared_storage.status_code == 200, prepared_storage.text
        assert prepared_storage.json()["can_prepare_storage"]
        storage = world.store.get(
            ProviderConnection, UUID(prepared_storage.json()["storage_connections"][0]["id"])
        )
        assert storage and storage.status == "READY"
        from aco.features.usage.models import ProviderUsageEvent

        assert (
            world.store.scalar(
                select(func.count())
                .select_from(ProviderUsageEvent)
                .where(ProviderUsageEvent.provider_connection_id == storage.id)
            )
            == 4
        )
        world.store.rollback()
        assert (
            world.client.post(base + "/voice-consents/storage/setup", json=storage_command).status_code == 200
        )
        assert (
            world.client.post(
                base + "/voice-consents/storage/setup",
                json=storage_command | {"evidence": "Changed same-request reviewed evidence"},
            ).status_code
            == 409
        )
        setup = world.client.post(base + "/voice-sessions/setup", json={"request_id": str(uuid4())})
        assert setup.status_code == 200, setup.text
        connection = setup.json()["connections"][0]["id"]
        invoice = world.store.get(InvoiceSnapshot, world.invoice_id)
        assert invoice
        consent_command = {
            "request_id": str(uuid4()),
            "customer_id": str(invoice.customer_id),
            "voice_connection_id": connection,
            "storage_connection_id": str(storage.id),
            "decision": "GRANTED",
            "scopes": {
                "audio_capture": True,
                "transcript": True,
                "private_storage": True,
                "playback": True,
                "provider_retention": True,
            },
            "purpose": "Explicit synthetic private capture verification",
            "jurisdiction": "AU-VIC",
            "notice": "Synthetic fixtures only, no customer recording or microphone access.",
            "evidence": "Current Owner reviewed separate processing purposes for this fixed fixture.",
            "expires_at": (now() + timedelta(days=1)).isoformat(),
        }
        world.store.rollback()
        consent = world.client.post(base + "/voice-consents", json=consent_command)
        assert consent.status_code == 201, consent.text
        prepared = world.client.post(
            base + "/voice-sessions",
            json={
                "request_id": str(uuid4()),
                "connection_id": connection,
                "invoice_ids": [str(world.invoice_id)],
                "objective": "Inspect synthetic sources and preserve only permitted fixtures",
                "consent_id": consent.json()["id"],
            },
        )
        assert prepared.status_code == 201, prepared.text
        view = prepared.json()
        reviewer, _ = add_actor(world.store, app, "capture-reviewer@synthetic.invalid")
        world.store.add(
            Membership(tenant_id=world.ctx.tenant_id, user_id=reviewer.id, role="finance_manager")
        )
        world.store.commit()
        reviewer_ctx = world.ctx.model_copy(update={"actor_id": reviewer.id, "role": Role.FINANCE_MANAGER})
        approvals = HumanApprovalService(world.store)
        approval = approvals.get(reviewer_ctx, UUID(view["approval_id"]))
        assert approval.proposal.model_dump()["consent_hash"] == consent.json()["material_hash"]
        approvals.decide_approval(
            reviewer_ctx,
            approval.id,
            ApprovalDecisionCommand(
                expected_version=approval.version,
                reviewed_action_version=approval.action_version,
                reviewed_material_hash=approval.material_hash,
                reviewed_binding_hash=approval.current_binding_hash,
                decision="approved",
                reason="Explicitly reviewed fixed synthetic capture scopes",
            ),
        )
        world.store.commit()
        started = await demo.runtime.start(world.ctx, UUID(view["id"]), view["version"])
        assert started.state == "completed" and len(started.changed_ids) == 1
        current = world.client.get(base + "/voice-sessions/" + view["id"]).json()
        assert current["can_reconcile"] and not current["reconciliation_reasons"]
        route = base + "/voice-sessions/" + view["id"] + "/reconcile"
        archived = world.client.post(route)
        assert archived.status_code == 200, archived.text
        saved = archived.json()
        assert saved["capture_evidence"] == "FAKE" and saved["communication_id"]
        assert saved["transcript_available"] and saved["recording_available"]
        assert len(saved["artifacts"]) == 2 and saved["changed_ids"] == [str(started.changed_ids[0])]
        replay = world.client.post(route)
        assert replay.status_code == 200 and replay.json()["artifacts"] == saved["artifacts"]
        world.store.expire_all()
        assert world.store.scalar(select(func.count()).select_from(CollectionTask)) == 1
        assert world.store.scalar(select(func.count()).select_from(Communication)) == 1
        assert world.store.scalar(select(func.count()).select_from(CommunicationInvoiceLink)) == 1
        assert world.store.scalar(select(func.count()).select_from(Artifact)) == 2
        world.store.rollback()
        for artifact in saved["artifacts"]:
            downloaded = world.client.get(base + f"/artifacts/{artifact['id']}/content")
            assert downloaded.status_code == 200, downloaded.text
            assert downloaded.headers["x-content-type-options"] == "nosniff"
            assert downloaded.content.startswith(
                b"RIFF" if artifact["content_type"] == "audio/wav" else b'{"synthetic"'
            )
        recording = next(item for item in saved["artifacts"] if item["content_type"] == "audio/wav")
        playback = world.client.get(base + f"/artifacts/{recording['id']}/playback")
        assert playback.status_code == 200 and playback.content.startswith(b"RIFF")
        assert playback.headers["content-type"] == "audio/wav"
        assert playback.headers["cache-control"] == "no-store"
        assert playback.headers["x-content-type-options"] == "nosniff"
        assert playback.headers["referrer-policy"] == "no-referrer"
        revoked = world.client.post(
            base + f"/voice-consents/{consent.json()['id']}/revoke",
            json={
                "request_id": str(uuid4()),
                "expected_version": consent.json()["version"],
                "reason": "Withdraw all fixed synthetic capture permission now",
            },
        )
        assert revoked.status_code == 200, revoked.text
        assert world.client.get(base + f"/artifacts/{recording['id']}/playback").status_code == 404
        for artifact in saved["artifacts"]:
            assert world.client.get(base + f"/artifacts/{artifact['id']}/content").status_code == 404
            if purge_path == "human":
                purge = world.client.post(base + f"/artifacts/{artifact['id']}/purge")
                assert purge.status_code == 200 and purge.json()["status"] == "deleted", purge.text
        if purge_path.startswith("worker"):
            from aco.core.settings import Settings
            from aco.features.artifacts.retention import dispatch_purges, recover_purges
            from aco.worker.app import WorkerRuntime

            config = storage_configuration()
            worker = WorkerRuntime(
                Settings(
                    database_url=SecretStr(world.url),
                    storage_endpoint=config["endpoint"],
                    storage_bucket=config["bucket"],
                    storage_access_key=config["access_key"],
                    storage_secret_key=config["secret_key"],
                    storage_local_endpoint_ip=config.get("local_endpoint_ip"),
                )
            )
            try:
                async with worker.app.open_async():
                    assert await dispatch_purges(worker.sessions, worker.app) == 2
                    if purge_path == "worker_read_race":
                        # Actual browser order: withdrawal -> maintenance enqueue -> denied read.
                        # Neither a denied download nor metadata inspection may revoke the job's lease.
                        world.store.expire_all()
                        queued = list(world.store.scalars(select(ArtifactPurge)))
                        assert len(queued) == 2 and all(row.status == "queued" for row in queued)
                        world.store.rollback()
                        for artifact in saved["artifacts"]:
                            assert (
                                world.client.get(base + f"/artifacts/{artifact['id']}/content").status_code
                                == 404
                            )
                            assert world.client.get(base + f"/artifacts/{artifact['id']}").status_code == 200
                    if purge_path == "worker_retained_pending":
                        # Exact retained state produced by the pre-repair queued-read race.
                        # Keep the genuine job/actor/object identity; never bypass a purge guard.
                        world.store.expire_all()
                        for retained in world.store.scalars(select(ArtifactPurge)):
                            assert retained.status == "queued" and retained.job_id is not None
                            retained.status = "purge_pending"
                        world.store.commit()
                    # A still-pending library job is not evidence that execution finished.
                    assert await recover_purges(worker.sessions) == 0
                    await worker.app.run_worker_async(
                        queues=["aco"], concurrency=1, wait=False, listen_notify=False
                    )
                    if purge_path == "worker_retained_pending":
                        # Worker maintenance may already have recovered/requeued this same
                        # retained state. Exercise recovery again without assuming scheduler order.
                        await recover_purges(worker.sessions)
                        await dispatch_purges(worker.sessions, worker.app)
                        await worker.app.run_worker_async(
                            queues=["aco"], concurrency=1, wait=False, listen_notify=False
                        )
                    if purge_path == "worker_read_race":
                        await recover_purges(worker.sessions)
                        await dispatch_purges(worker.sessions, worker.app)
                        world.store.expire_all()
                        actual = list(
                            world.store.execute(
                                text(
                                    "SELECT p.status, j.status::text AS library_status "
                                    "FROM artifact_purge_requests p "
                                    "LEFT JOIN procrastinate_jobs j ON j.id=p.job_id "
                                    "ORDER BY p.id"
                                )
                            )
                        )
                        assert len(actual) == 2 and all(row.status == "deleted" for row in actual), actual
                        world.store.rollback()
            finally:
                await worker.engine.dispose()
        assert world.client.post(route).status_code in {403, 410}
        world.store.expire_all()
        assert all(
            row.status == "deleted" and row.provider_receipt
            for row in world.store.scalars(select(ArtifactPurge))
        )
        if purge_path == "worker_retained_pending":
            assert all(row.job_version == 2 for row in world.store.scalars(select(ArtifactPurge)))
        from aco.features.artifacts.consent_models import VoiceArtifactLink

        assert all(
            link.provider_purged_at and link.provider_purge_receipt
            for link in world.store.scalars(select(VoiceArtifactLink))
        )
        assert world.store.scalar(select(func.count()).select_from(CollectionTask)) == 1
        await engine.dispose()
