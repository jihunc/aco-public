"""Exact normative voice boundary outputs from real refusal/session use cases."""

import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from action_support import action_world
from boundary_evidence import BoundaryObserver
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from voice_support import exercise_voice


def fixtures() -> list[dict[str, object]]:
    value = json.loads((Path(__file__).parents[1] / "fixtures/policies/boundaries.json").read_text())
    rows = value if isinstance(value, list) else value["cases"]
    return [row for row in rows if row["domain"] == "voice"]


async def test_boundary_voice_capture_and_gate_classification(boundary_observer: BoundaryObserver) -> None:
    from aco.features.artifacts.models import Artifact
    from aco.features.identity.models import ProviderConnection
    from aco.features.voice.dto import PrepareVoice
    from aco.features.voice.models import VoiceSession
    from aco.features.voice.runtime import VoiceRuntime
    from aco.features.voice.service import VoiceService
    from aco.integrations.vapi.settings import VapiBinding

    cases = fixtures()
    assert {row["id"] for row in cases} == {
        "voice_capture_off_provider_not_verified",
        "voice_browser_not_number_call_evidence",
    }
    by_id = {str(row["id"]): row for row in cases}
    with action_world() as world:
        connection = ProviderConnection(
            tenant_id=world.ctx.tenant_id,
            provider="vapi",
            provider_type="voice",
            status="READY",
            encrypted_credentials_ref="unapproved-live",
        )
        world.store.add(connection)
        world.store.commit()
        binding = VapiBinding(
            tenant_id=world.ctx.tenant_id,
            connection_id=connection.id,
            assistant_id=uuid4(),
            api_key=SecretStr("not-a-real-key"),
            webhook_secret=SecretStr("not-a-real-secret"),
            credential_revision="unapproved-live",
        )

        def forbidden(request: httpx.Request) -> httpx.Response:
            pytest.fail("No live voice request is authorized")

        runtime = VoiceRuntime(
            async_sessionmaker(),
            lambda tenant, conn: binding,
            enabled=True,
            transport_factory=lambda: httpx.MockTransport(forbidden),
            capability_secret=SecretStr("offline"),
        )
        with pytest.raises(HTTPException) as denied:
            VoiceService(world.store, runtime).prepare_call(
                world.ctx,
                PrepareVoice(
                    request_id=uuid4(),
                    connection_id=connection.id,
                    invoice_ids=[world.invoice_id],
                    objective="Unavailable live no-capture",
                ),
            )
        assert denied.value.detail == "VOICE_LIVE_NOT_AUTHORIZED"
        assert world.store.scalar(select(func.count()).select_from(VoiceSession)) == 0
        assert world.store.scalar(select(func.count()).select_from(Artifact)) == 0
        actual = {
            "live_session_allowed": bool(world.store.scalar(select(func.count()).select_from(VoiceSession))),
            "stored_audio": bool(
                world.store.scalar(
                    select(func.count()).select_from(Artifact).where(Artifact.content_type.like("audio/%"))
                )
            ),
            "stored_transcript": bool(
                world.store.scalar(
                    select(func.count())
                    .select_from(Artifact)
                    .where(Artifact.content_type == "application/json")
                )
            ),
            "live_status": "BLOCKED" if denied.value.detail == "VOICE_LIVE_NOT_AUTHORIZED" else "UNEXPECTED",
        }
        assert actual == by_id["voice_capture_off_provider_not_verified"]["expected"]
        boundary_observer.assert_observed("voice_capture_off_provider_not_verified", actual)
    detail = await exercise_voice("success")
    assert detail
    actual2 = {
        "AC-23_live_result": detail.live_status,
        "P-VOICE-02_result": detail.pstn_status,
        "stored_recording_clause_proven": detail.recording_available,
    }
    assert actual2 == by_id["voice_browser_not_number_call_evidence"]["expected"]
    boundary_observer.assert_observed("voice_browser_not_number_call_evidence", actual2)
