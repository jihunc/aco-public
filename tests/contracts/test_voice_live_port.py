"""Offline HTTP verification of the gated LIVE code path; not real provider evidence."""

import json
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr

from aco.core.types import ProviderErrorCode
from aco.features.identity.models import now
from aco.integrations.ports.voice import (
    CallContext,
    CallObjective,
    ConsentMode,
    VoiceFailure,
    VoiceSetupContext,
)
from aco.integrations.vapi.adapter import VapiVoiceAdapter
from aco.integrations.vapi.settings import VapiBinding


class Observer:
    def __init__(self) -> None:
        self.started_operations: list[str] = []
        self.finished_operations: list[tuple[str, str | None]] = []

    async def started(self, ctx: CallContext | VoiceSetupContext, operation: str) -> UUID:
        self.started_operations.append(operation)
        return uuid4()

    async def finished(self, key: UUID, operation: str, *, native: str | None, failure: str | None) -> None:
        self.finished_operations.append((operation, failure))


@pytest.mark.parametrize("lost_response", [False, True])
async def test_live_branch_is_one_create_capture_off_and_observes_actual_attempt(lost_response: bool) -> None:
    tenant, connection, session, actor, invoice, assistant, native = [uuid4() for _ in range(7)]
    observer = Observer()
    calls: list[httpx.Request] = []
    checks: list[str] = []

    async def authorize(ctx: CallContext, operation: str, material: object) -> None:
        checks.append(operation)

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = json.loads(request.content)
        assert request.method == "POST" and request.url.path == "/call/web"
        override = body["assistantOverrides"]
        assert "server" not in override and "customer" not in body and "phoneNumberId" not in body
        assert override["maxDurationSeconds"] == 120
        assert override["artifactPlan"] == {
            "recordingEnabled": False,
            "loggingEnabled": False,
            "videoRecordingEnabled": False,
            "transcriptPlan": {"enabled": False},
        }
        assert override["firstMessage"].startswith("This is an AI assistant")
        assert "synthetic offline" not in override["firstMessage"]
        if lost_response:
            raise httpx.ReadTimeout("Offline lost response", request=request)
        return httpx.Response(
            201,
            json={
                "id": str(native),
                "assistantId": str(assistant),
                "type": "webCall",
                "status": "queued",
                "webCallUrl": "https://vapi.daily.co/owned-room",
            },
        )

    binding = VapiBinding(
        tenant_id=tenant,
        connection_id=connection,
        assistant_id=assistant,
        api_key=SecretStr("offline-not-a-key"),
        webhook_secret=SecretStr("offline-signing-secret"),
        credential_revision="offline-live-profile",
        evidence_mode="LIVE",
        max_duration_seconds=120,
    )
    adapter = VapiVoiceAdapter(
        binding,
        authority=authorize,
        enabled=True,
        transport=httpx.MockTransport(transport),
        observer=observer,
    )
    ctx = CallContext(
        tenant_id=tenant,
        connection_id=connection,
        session_id=session,
        actor_id=actor,
        correlation_id=uuid4(),
        expires_at=now() + timedelta(minutes=5),
    )
    objective = CallObjective(
        primary_invoice_id=invoice,
        invoice_ids=(invoice,),
        objective="Review synthetic business facts",
        source_as_of=now(),
    )
    if lost_response:
        with pytest.raises(VoiceFailure) as failure:
            await adapter.start_call(
                objective, ctx, ("get_invoice",), ConsentMode(participant="LIVE_HUMAN_MICROPHONE_CAPTURE_OFF")
            )
        assert failure.value.failure.code == ProviderErrorCode.UNKNOWN_SIDE_EFFECT
        assert failure.value.failure.may_have_side_effect
    else:
        result = await adapter.start_call(
            objective, ctx, ("get_invoice",), ConsentMode(participant="LIVE_HUMAN_MICROPHONE_CAPTURE_OFF")
        )
        assert (
            result.reference.external_id == str(native)
            and not result.synthetic
            and result.evidence_mode == "LIVE"
        )
        assert (
            result.browser_join is not None
            and result.browser_join.get_secret_value() == "https://vapi.daily.co/owned-room"
        )
        assert "owned-room" not in result.model_dump_json() and result.cost.amount is None
    assert len(calls) == 1 and observer.started_operations == ["start_browser"]
    assert len(observer.finished_operations) == 1
    assert bool(observer.finished_operations[0][1]) is lost_response
    assert checks.count("start_call") == 2  # Fresh recheck after durable intent, immediately before wire.


def test_live_duration_changes_the_immutable_binding_material() -> None:
    from aco.features.voice.runtime import VoiceRuntime

    binding = VapiBinding(
        tenant_id=uuid4(),
        connection_id=uuid4(),
        assistant_id=uuid4(),
        api_key=SecretStr("offline-not-a-key"),
        webhook_secret=SecretStr("offline-signing-secret"),
        credential_revision="offline-live-profile",
        evidence_mode="LIVE",
        max_duration_seconds=120,
    )
    assert VoiceRuntime.binding_hash(binding) != VoiceRuntime.binding_hash(
        binding.model_copy(update={"max_duration_seconds": 300})
    )


@pytest.mark.parametrize(
    "join_url", ["http://vapi.daily.co/room", "https://vapi.daily.co:bad/room", "https://evil.invalid/room"]
)
async def test_accepted_native_is_retained_when_join_capability_is_unsafe(join_url: str) -> None:
    tenant, connection, assistant, native = [uuid4() for _ in range(4)]
    observer = Observer()

    async def authorize(ctx: CallContext, operation: str, material: object) -> None:
        pass

    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "id": str(native),
                "assistantId": str(assistant),
                "type": "webCall",
                "status": "queued",
                "webCallUrl": join_url,
            },
        )

    adapter = VapiVoiceAdapter(
        VapiBinding(
            tenant_id=tenant,
            connection_id=connection,
            assistant_id=assistant,
            api_key=SecretStr("offline-only"),
            webhook_secret=SecretStr("offline-only"),
            credential_revision="offline",
            evidence_mode="LIVE",
        ),
        authority=authorize,
        enabled=True,
        transport=httpx.MockTransport(transport),
        observer=observer,
    )
    invoice = uuid4()
    result = await adapter.start_call(
        CallObjective(
            primary_invoice_id=invoice, invoice_ids=(invoice,), objective="Offline data", source_as_of=now()
        ),
        CallContext(
            tenant_id=tenant,
            connection_id=connection,
            session_id=uuid4(),
            actor_id=uuid4(),
            correlation_id=uuid4(),
            expires_at=now() + timedelta(minutes=5),
        ),
        ("get_invoice",),
        ConsentMode(participant="LIVE_HUMAN_MICROPHONE_CAPTURE_OFF"),
    )
    assert result.reference.external_id == str(native) and result.browser_join is None
    assert observer.started_operations == ["start_browser"] and observer.finished_operations == [
        ("start_browser", None)
    ]
