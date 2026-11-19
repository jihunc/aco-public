"""Regression from C independent consent review: provider results never expand saved consent."""

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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aco.core.types import Role
from aco.features.accounting.models import InvoiceSnapshot
from aco.features.artifacts.models import Artifact
from aco.features.identity.models import Membership, ProviderConnection, now
from aco.features.review.approval_dto import ApprovalDecisionCommand
from aco.features.review.approvals import HumanApprovalService
from aco.features.voice.demo import OfflineVoiceDemo
from aco.integrations.ports.voice_artifacts import VoiceArtifactAuthority, VoiceArtifactRequest, VoiceOriginal
from aco.integrations.s3.adapter import StorageRuntime


@pytest.mark.parametrize("mismatch", ["kind", "mime", "scope", "time", "disabled", "body"])
async def test_transcript_only_consent_refuses_provider_recording(
    monkeypatch: pytest.MonkeyPatch, mismatch: str
) -> None:
    import io
    import wave

    from aco.integrations.fake.voice_artifacts import SyntheticVoiceArtifacts

    original_fetch = SyntheticVoiceArtifacts.fetch

    async def mismatched_fetch(
        self: SyntheticVoiceArtifacts, request: VoiceArtifactRequest, authorize: VoiceArtifactAuthority
    ) -> VoiceOriginal:
        original = await original_fetch(self, request, authorize)
        assert request.kind == "transcript"
        if mismatch == "body":
            return original.model_copy(update={"body": b"RIFFsynthetic-audio-not-a-transcript"})
        if mismatch == "scope":
            return original.model_copy(update={"request": request.model_copy(update={"session_id": uuid4()})})
        if mismatch == "time":
            return original.model_copy(update={"captured_at": request.captured_at + timedelta(seconds=1)})
        if mismatch == "disabled":
            demo.runtime.enabled = False
            return original
        output = io.BytesIO()
        with wave.open(output, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b"\0" * 16000)
        return original.model_copy(
            update={
                "kind": "recording" if mismatch == "kind" else "transcript",
                "content_type": "audio/wav",
                "body": output.getvalue(),
            }
        )

    monkeypatch.setattr(SyntheticVoiceArtifacts, "fetch", mismatched_fetch)
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
        # Integration fixture connection only; public browser setup must run the actual readiness probe.
        storage = ProviderConnection(
            tenant_id=world.ctx.tenant_id, provider="s3", provider_type="storage", status="READY"
        )
        world.store.add(storage)
        world.store.commit()
        base = f"/api/workspaces/{world.ctx.tenant_id}"
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
                "audio_capture": False,
                "transcript": True,
                "private_storage": True,
                "playback": False,
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
        route = base + "/voice-sessions/" + view["id"] + "/reconcile"
        archived = world.client.post(route)
        assert archived.status_code in {403, 422}, archived.text
        world.store.expire_all()
        assert world.store.scalar(select(func.count()).select_from(Artifact)) == 0
        await engine.dispose()
