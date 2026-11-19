"""Voice uses real HTTPX mapping with explicitly simulated external responses."""

import hashlib
import hmac
import json
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from aco.core.clock import SystemClock
from aco.core.types import ProviderErrorCode


async def test_browser_adapter_disables_all_capture_and_retains_native_identity() -> None:
    from aco.integrations.ports.voice import CallContext, CallObjective, ConsentMode, VoiceFailure, VoicePort
    from aco.integrations.vapi.adapter import VapiVoiceAdapter
    from aco.integrations.vapi.settings import VapiBinding

    tenant, connection, session, actor, invoice, native = (uuid4() for _ in range(6))
    calls: list[str] = []
    authority_calls: list[str] = []

    async def authorize(ctx: CallContext, operation: str, material: object) -> None:
        assert ctx.tenant_id == tenant and ctx.session_id == session
        authority_calls.append(operation)

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.headers["authorization"] == "Bearer offline-not-a-key"
        if request.method == "POST":
            body = json.loads(request.content)
            assert request.url.path == "/call/web"
            plan = body["assistantOverrides"]["artifactPlan"]
            assert plan == {
                "recordingEnabled": False,
                "loggingEnabled": False,
                "videoRecordingEnabled": False,
                "transcriptPlan": {"enabled": False},
            }
            assert "server" not in body["assistantOverrides"]
            assert body["assistantId"] == str(binding.assistant_id)
            assert "phoneNumberId" not in body and "customer" not in body
        return httpx.Response(
            201,
            json={
                "id": str(native),
                "type": "webCall",
                "status": "queued",
                "assistantId": str(binding.assistant_id),
            },
        )

    binding = VapiBinding(
        tenant_id=tenant,
        connection_id=connection,
        assistant_id=uuid4(),
        api_key=SecretStr("offline-not-a-key"),
        webhook_secret=SecretStr("mock-secret"),
        credential_revision="offline-1",
        evidence_mode="MOCKED_HTTP",
    )
    adapter = VapiVoiceAdapter(
        binding, authority=authorize, enabled=True, transport=httpx.MockTransport(transport)
    )
    assert isinstance(adapter, VoicePort)
    ctx = CallContext(
        tenant_id=tenant,
        connection_id=connection,
        session_id=session,
        actor_id=actor,
        correlation_id=uuid4(),
        expires_at=SystemClock().now() + timedelta(minutes=5),
    )
    objective = CallObjective(
        primary_invoice_id=invoice,
        invoice_ids=(invoice,),
        objective="Review synthetic invoice",
        source_as_of=SystemClock().now(),
    )
    result = await adapter.start_call(
        objective, ctx, ("get_invoice", "create_collection_task"), ConsentMode()
    )
    assert result.reference.external_id == str(native) and result.synthetic
    assert result.evidence_mode == "MOCKED_HTTP" and result.cost.amount is None
    read = await adapter.get_call(ctx, result.reference)
    assert read.reference == result.reference and len(calls) == 2
    assert authority_calls == ["start_call", "get_call"]
    with pytest.raises(VoiceFailure) as refused:
        await adapter.get_transcript(ctx, result.reference)
    assert refused.value.failure.code == ProviderErrorCode.UNSUPPORTED_CAPABILITY
    assert len(calls) == 2


@pytest.mark.parametrize(
    "mode,enabled,transport_present",
    [("LIVE", True, True), ("MOCKED_HTTP", False, True), ("MOCKED_HTTP", True, False)],
)
async def test_voice_cannot_open_live_or_default_disabled_wire(
    mode: str, enabled: bool, transport_present: bool
) -> None:
    from aco.integrations.ports.voice import CallContext, CallObjective, ConsentMode, VoiceFailure
    from aco.integrations.vapi.adapter import VapiVoiceAdapter
    from aco.integrations.vapi.settings import VapiBinding

    def forbidden(request: httpx.Request) -> httpx.Response:
        pytest.fail("blocked voice must not send a request")

    async def authority(ctx: CallContext, operation: str, material: object) -> None:
        pytest.fail("blocked voice must not acquire an application grant")

    binding = VapiBinding.model_validate(
        dict(
            tenant_id=uuid4(),
            connection_id=uuid4(),
            assistant_id=uuid4(),
            api_key=SecretStr("offline"),
            webhook_secret=SecretStr("mock-secret"),
            credential_revision="1",
            evidence_mode=mode,
        )
    )
    adapter = VapiVoiceAdapter(
        binding,
        authority=authority,
        enabled=enabled,
        transport=httpx.MockTransport(forbidden) if transport_present else None,
    )
    ctx = CallContext(
        tenant_id=binding.tenant_id,
        connection_id=binding.connection_id,
        session_id=uuid4(),
        actor_id=uuid4(),
        correlation_id=uuid4(),
        expires_at=SystemClock().now() + timedelta(minutes=5),
    )
    invoice = uuid4()
    with pytest.raises(VoiceFailure):
        await adapter.start_call(
            CallObjective(
                primary_invoice_id=invoice,
                invoice_ids=(invoice,),
                objective="Synthetic",
                source_as_of=SystemClock().now(),
            ),
            ctx,
            ("get_invoice",),
            ConsentMode(),
        )


async def test_hmac_exact_call_timestamp_and_capture_callbacks_are_enforced() -> None:
    from aco.integrations.ports.voice import CallContext, VoiceFailure
    from aco.integrations.vapi.adapter import VapiVoiceAdapter
    from aco.integrations.vapi.settings import VapiBinding

    async def authority(ctx: CallContext, operation: str, material: object) -> None:
        pass

    binding = VapiBinding(
        tenant_id=uuid4(),
        connection_id=uuid4(),
        assistant_id=uuid4(),
        api_key=SecretStr("offline"),
        webhook_secret=SecretStr("test-secret"),
        credential_revision="1",
        evidence_mode="MOCKED_HTTP",
    )
    adapter = VapiVoiceAdapter(
        binding,
        authority=authority,
        enabled=True,
        transport=httpx.MockTransport(lambda _: httpx.Response(500)),
    )
    native = uuid4()
    ctx = CallContext(
        tenant_id=binding.tenant_id,
        connection_id=binding.connection_id,
        session_id=uuid4(),
        actor_id=uuid4(),
        correlation_id=uuid4(),
        expires_at=SystemClock().now() + timedelta(minutes=5),
        provider_call_id=str(native),
    )
    payload = {
        "message": {
            "type": "tool-calls",
            "call": {"id": str(native), "assistantId": str(binding.assistant_id)},
            "toolCallList": [
                {
                    "id": "native-tool-1",
                    "type": "function",
                    "function": {"name": "get_invoice", "arguments": {"invoice_id": str(uuid4())}},
                }
            ],
        }
    }
    raw = json.dumps(payload).encode()
    timestamp = str(int(SystemClock().now().timestamp()))
    headers = {
        "x-timestamp": timestamp,
        "x-signature": hmac.new(b"test-secret", timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest(),
    }
    verified = await adapter.verify_webhook(ctx, raw, headers)
    events = adapter.normalize_call_event(verified)
    assert len(events) == 1 and events[0].invocation_id == "native-tool-1"
    for bad_raw, bad_headers, bad_ctx in [
        (raw + b" ", headers, ctx),
        (raw, headers, ctx.model_copy(update={"provider_call_id": str(uuid4())})),
        (raw, headers | {"x-timestamp": "1"}, ctx),
    ]:
        with pytest.raises(VoiceFailure):
            await adapter.verify_webhook(bad_ctx, bad_raw, bad_headers)
    payload["message"] = {
        "type": "transcript",
        "call": {"id": str(native)},
        "transcript": "Must never retain",
    }
    raw = json.dumps(payload).encode()
    headers["x-signature"] = hmac.new(
        b"test-secret", timestamp.encode() + b"." + raw, hashlib.sha256
    ).hexdigest()
    with pytest.raises(VoiceFailure):
        await adapter.verify_webhook(ctx, raw, headers)


async def test_start_transport_timeout_is_uncertain_and_never_retried() -> None:
    from aco.integrations.ports.voice import CallContext, CallObjective, ConsentMode, VoiceFailure
    from aco.integrations.vapi.adapter import VapiVoiceAdapter
    from aco.integrations.vapi.settings import VapiBinding

    count = 0

    def transport(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    async def authority(ctx: CallContext, operation: str, material: object) -> None:
        pass

    b = VapiBinding(
        tenant_id=uuid4(),
        connection_id=uuid4(),
        assistant_id=uuid4(),
        api_key=SecretStr("offline"),
        webhook_secret=SecretStr("offline"),
        credential_revision="1",
        evidence_mode="MOCKED_HTTP",
    )
    a = VapiVoiceAdapter(b, authority=authority, enabled=True, transport=httpx.MockTransport(transport))
    ctx = CallContext(
        tenant_id=b.tenant_id,
        connection_id=b.connection_id,
        session_id=uuid4(),
        actor_id=uuid4(),
        correlation_id=uuid4(),
        expires_at=SystemClock().now() + timedelta(minutes=5),
    )
    invoice = uuid4()
    with pytest.raises(VoiceFailure) as failed:
        await a.start_call(
            CallObjective(
                primary_invoice_id=invoice,
                invoice_ids=(invoice,),
                objective="Synthetic",
                source_as_of=SystemClock().now(),
            ),
            ctx,
            ("get_invoice",),
            ConsentMode(),
        )
    assert count == 1 and failed.value.failure.may_have_side_effect and not failed.value.failure.retryable


async def test_second_voice_adapter_shape_preserves_port_and_tenant_scope() -> None:
    from aco.integrations.fake.voice import FakeVoiceAdapter
    from aco.integrations.ports.voice import CallContext, CallObjective, ConsentMode, VoiceFailure, VoicePort

    async def authority(ctx: CallContext, operation: str, material: object) -> None:
        pass

    tenant, connection, invoice = uuid4(), uuid4(), uuid4()
    adapter = FakeVoiceAdapter(tenant, connection, authority=authority)
    assert isinstance(adapter, VoicePort)
    ctx = CallContext(
        tenant_id=tenant,
        connection_id=connection,
        session_id=uuid4(),
        actor_id=uuid4(),
        correlation_id=uuid4(),
        expires_at=SystemClock().now() + timedelta(minutes=1),
    )
    objective = CallObjective(
        primary_invoice_id=invoice,
        invoice_ids=(invoice,),
        objective="Synthetic fake session",
        source_as_of=SystemClock().now(),
    )
    call = await adapter.start_call(objective, ctx, ("get_invoice",), ConsentMode())
    assert call.evidence_mode == "FAKE" and call.reference.provider == "fake_voice"
    assert (await adapter.get_call(ctx, call.reference)) == call
    assert await adapter.start_call(objective, ctx, ("get_invoice",), ConsentMode()) == call
    with pytest.raises(VoiceFailure):
        await adapter.get_call(ctx.model_copy(update={"tenant_id": uuid4()}), call.reference)
    with pytest.raises(VoiceFailure):
        await adapter.get_recording_if_permitted(ctx, call.reference)
