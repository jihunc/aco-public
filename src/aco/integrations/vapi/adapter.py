"""Vapi HTTPX browser mapping; LIVE additionally requires a per-wire authority observer.

Configured HMAC profile: SHA256, hex, x-signature, x-timestamp, {timestamp}.{body}.
Saved assistant configuration owns the server URL/credential; no transient override.
"""

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from aco.core.clock import Clock, SystemClock
from aco.core.types import (
    CostQuality,
    ProviderCapabilities,
    ProviderCost,
    ProviderErrorCode,
    ProviderFailure,
    ProviderReference,
)
from aco.integrations.ports.voice import (
    VOICE_TOOLS,
    CallContext,
    CallObjective,
    ConsentMode,
    ProviderCallRef,
    VerifiedVoiceWebhook,
    VoiceAuthority,
    VoiceEvent,
    VoiceFailure,
    VoiceSetupAuthority,
    VoiceSetupContext,
    VoiceTool,
)
from aco.integrations.ports.voice_observer import VoiceRequestObserver
from aco.integrations.vapi.settings import VapiBinding


def refused(
    code: ProviderErrorCode = ProviderErrorCode.VALIDATION_FAILED, *, uncertain: bool = False
) -> VoiceFailure:
    return VoiceFailure(ProviderFailure(code=code, retryable=False, may_have_side_effect=uncertain))


class _Call(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: UUID
    assistantId: UUID
    type: str
    status: str
    webCallUrl: SecretStr | None = None
    transport: dict[str, object] | None = None
    monitor: dict[str, object] | None = None


class VapiVoiceAdapter:
    def __init__(
        self,
        binding: VapiBinding,
        *,
        authority: VoiceAuthority,
        enabled: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock | None = None,
        observer: VoiceRequestObserver | None = None,
    ):
        self.binding, self.authority, self.enabled, self.transport = binding, authority, enabled, transport
        self.connection_id = binding.connection_id
        self.clock = clock or SystemClock()
        self.observer = observer

    def _scope(self, ctx: CallContext | VoiceSetupContext, *, terminal_verification: bool = False) -> None:
        if (
            not self.enabled
            or (self.binding.evidence_mode == "LIVE" and self.observer is None)
            or self.transport is None
            or ctx.tenant_id != self.binding.tenant_id
            or ctx.connection_id != self.connection_id
            or (self.clock.now() >= ctx.expires_at and not terminal_verification)
        ):
            raise refused(ProviderErrorCode.AUTH_EXPIRED)

    async def probe_assistant(self, ctx: VoiceSetupContext, authority: VoiceSetupAuthority) -> None:
        """Connection probe only; fresh human setup authority never authorizes a call."""
        self._scope(ctx)
        if self.binding.server_credential_id is None:
            raise refused(ProviderErrorCode.UNSUPPORTED_CAPABILITY)
        await authority(ctx)
        try:
            async with httpx.AsyncClient(
                transport=self.transport, trust_env=False, follow_redirects=False, timeout=10
            ) as client:
                async with client.stream(
                    "GET",
                    "https://api.vapi.ai/assistant/" + str(self.binding.assistant_id),
                    headers={"Authorization": "Bearer " + self.binding.api_key.get_secret_value()},
                ) as response:
                    if response.status_code != 200:
                        raise refused(ProviderErrorCode.TEMPORARILY_UNAVAILABLE)
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 131072:
                            raise ValueError("Oversized assistant probe")
                    value = json.loads(raw)
                    plan, server = value.get("artifactPlan", {}), value.get("server", {})
                    if (
                        value.get("id") != str(self.binding.assistant_id)
                        or server.get("credentialId") != str(self.binding.server_credential_id)
                        or plan.get("recordingEnabled") is not False
                        or plan.get("loggingEnabled") is not False
                        or plan.get("videoRecordingEnabled") is not False
                        or plan.get("transcriptPlan", {}).get("enabled") is not False
                    ):
                        raise ValueError("Assistant capture/configuration mismatch")
        except (httpx.TransportError, ValueError, TypeError, AttributeError) as exc:
            raise refused(ProviderErrorCode.VALIDATION_FAILED) from exc

    async def _request(
        self,
        ctx: CallContext,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        *,
        material: object = None,
    ) -> _Call:
        self._scope(ctx)
        operation = "start_browser" if method == "POST" else "get_call"
        key = await self.observer.started(ctx, operation) if self.observer else None
        native = None
        failure: str | None = "NO_EFFECT"
        attempted = False
        try:
            if self.binding.evidence_mode == "LIVE":
                await self.authority(ctx, "start_call" if method == "POST" else "get_call", material)
            attempted = True
            failure = "UNKNOWN_SIDE_EFFECT" if method == "POST" else "TEMPORARILY_UNAVAILABLE"
            async with httpx.AsyncClient(
                transport=self.transport, trust_env=False, follow_redirects=False, timeout=10
            ) as client:
                async with client.stream(
                    method,
                    "https://api.vapi.ai" + path,
                    headers={"Authorization": "Bearer " + self.binding.api_key.get_secret_value()},
                    json=payload,
                ) as response:
                    if response.status_code >= 500 or response.status_code in {408, 409}:
                        raise refused(
                            ProviderErrorCode.UNKNOWN_SIDE_EFFECT
                            if method == "POST"
                            else ProviderErrorCode.TEMPORARILY_UNAVAILABLE,
                            uncertain=method == "POST",
                        )
                    if response.status_code not in {200, 201}:
                        raise refused(
                            ProviderErrorCode.AUTH_EXPIRED
                            if response.status_code in {401, 403}
                            else ProviderErrorCode.RATE_LIMITED
                            if response.status_code == 429
                            else ProviderErrorCode.VALIDATION_FAILED
                        )
                    raw = bytearray()
                    async for part in response.aiter_bytes():
                        raw.extend(part)
                        if len(raw) > 131072:
                            raise ValueError("Oversized call response")
                    call = _Call.model_validate_json(raw)
                    if (
                        call.assistantId != self.binding.assistant_id
                        or call.type != "webCall"
                        or call.status not in {"queued", "ringing", "in-progress", "ended"}
                    ):
                        raise ValueError("Unexpected call identity")
                    native, failure = str(call.id), None
                    return call
        except VoiceFailure as exc:
            failure = exc.failure.code.value
            raise
        except (httpx.TransportError, ValidationError, ValueError) as exc:
            raise refused(
                ProviderErrorCode.UNKNOWN_SIDE_EFFECT
                if method == "POST"
                else ProviderErrorCode.TEMPORARILY_UNAVAILABLE,
                uncertain=method == "POST",
            ) from exc
        finally:
            if self.observer is not None and key is not None:
                try:
                    await self.observer.finished(key, operation, native=native, failure=failure)
                except Exception as exc:
                    raise refused(ProviderErrorCode.UNKNOWN_SIDE_EFFECT, uncertain=attempted) from exc

    def _result(self, call: _Call) -> ProviderCallRef:
        return ProviderCallRef.model_validate(
            dict(
                reference=ProviderReference(
                    provider="vapi", connection_id=self.connection_id, external_id=str(call.id)
                ),
                status=call.status,
                evidence_mode=self.binding.evidence_mode,
                synthetic=self.binding.evidence_mode != "LIVE",
                browser_join=self._join(call) if self.binding.evidence_mode == "LIVE" else None,
                control_url=self._control(call) if self.binding.evidence_mode == "LIVE" else None,
                cost=ProviderCost(amount=None, currency="USD", quality=CostQuality.UNKNOWN),
            )
        )

    def _join(self, call: _Call) -> SecretStr | None:
        from urllib.parse import urlsplit

        transport = call.transport or {}
        if transport.get("callToken") is not None:
            # Recording-capable tokens are not compatible with this capture-off grant.
            return None
        value = call.webCallUrl.get_secret_value() if call.webCallUrl else transport.get("callUrl")
        if value is None:
            return None
        if not isinstance(value, str):
            return None
        try:
            target = urlsplit(value)
            if (
                target.scheme != "https"
                or target.hostname != "vapi.daily.co"
                or target.port not in {None, 443}
                or target.username
                or target.password
                or target.query
                or target.fragment
                or not re.fullmatch(r"/[A-Za-z0-9_-]+", target.path)
                or len(value) > 2000
            ):
                return None
        except ValueError:
            return None
        return SecretStr(value)

    def _control(self, call: _Call) -> SecretStr | None:
        value = (call.monitor or {}).get("controlUrl")
        if value is None:
            return None
        try:
            value = self.validate_control(value, str(call.id))
        except (ValueError, VoiceFailure):
            return None  # Known native call remains retained; no unsafe capability escapes.
        return SecretStr(value)

    def validate_control(self, value: object, native: str) -> str:
        from urllib.parse import urlsplit

        if not isinstance(value, str) or len(value) > 2000:
            raise refused()
        target = urlsplit(value)
        if (
            target.scheme != "https"
            or target.hostname not in self.binding.control_hosts
            or not target.hostname
            or not target.hostname.endswith(".vapi.ai")
            or target.port not in {None, 443}
            or target.username
            or target.password
            or target.query
            or target.fragment
            or target.path != f"/{UUID(native)}/control"
        ):
            raise refused()
        return value

    async def end_call(self, ctx: CallContext, control: SecretStr) -> None:
        # Actual provider-returned capability only; never construct a vendor URL.
        if (
            self.binding.evidence_mode != "LIVE"
            or self.binding.control_api_key is None
            or self.observer is None
        ):
            raise refused(ProviderErrorCode.UNSUPPORTED_CAPABILITY)
        if ctx.provider_call_id is None:
            raise refused()
        value = self.validate_control(control.get_secret_value(), ctx.provider_call_id)
        key = await self.observer.started(ctx, "end_call")
        failure: str | None = "NO_EFFECT"
        attempted = False
        try:
            await self.authority(ctx, "end_call", ctx.provider_call_id)
            attempted, failure = True, "UNKNOWN_SIDE_EFFECT"
            async with httpx.AsyncClient(
                transport=self.transport, trust_env=False, follow_redirects=False, timeout=10
            ) as client:
                response = await client.post(
                    value,
                    headers={"Authorization": "Bearer " + self.binding.control_api_key.get_secret_value()},
                    json={"type": "end-call"},
                )
                if (
                    response.status_code != 200
                    or len(response.content) > 16384
                    or response.json() != {"status": "ok"}
                ):
                    raise refused(ProviderErrorCode.UNKNOWN_SIDE_EFFECT, uncertain=True)
            failure = None
        except (httpx.TransportError, ValueError) as exc:
            raise refused(ProviderErrorCode.UNKNOWN_SIDE_EFFECT, uncertain=True) from exc
        finally:
            try:
                await self.observer.finished(key, "end_call", native=ctx.provider_call_id, failure=failure)
            except Exception as exc:
                raise refused(ProviderErrorCode.UNKNOWN_SIDE_EFFECT, uncertain=attempted) from exc

    async def start_call(
        self,
        objective: CallObjective,
        context: CallContext,
        allowed_tools: tuple[VoiceTool, ...],
        consent_mode: ConsentMode,
    ) -> ProviderCallRef:
        self._scope(context)
        if not allowed_tools or not set(allowed_tools) <= set(VOICE_TOOLS):
            raise refused()
        if consent_mode.participant != (
            "LIVE_HUMAN_MICROPHONE_CAPTURE_OFF"
            if self.binding.evidence_mode == "LIVE"
            else "SYNTHETIC_OFFLINE"
        ):
            raise refused()
        await self.authority(context, "start_call", objective)
        payload: dict[str, object] = {
            "assistantId": str(self.binding.assistant_id),
            "assistantOverrides": {
                "artifactPlan": {
                    "recordingEnabled": False,
                    "loggingEnabled": False,
                    "videoRecordingEnabled": False,
                    "transcriptPlan": {"enabled": False},
                },
                "maxDurationSeconds": self.binding.max_duration_seconds,
                "serverMessages": ["status-update", "tool-calls"]
                if self.binding.evidence_mode == "LIVE"
                else ["status-update", "tool-calls", "end-of-call-report"],
                "firstMessage": "This is an AI assistant in a synthetic offline collections demonstration."
                if self.binding.evidence_mode == "MOCKED_HTTP"
                else (
                    "This is an AI assistant in an approved browser voice test. "
                    "ACO does not retain a recording or transcript of this call."
                ),
                "variableValues": {
                    "acoSessionId": str(context.session_id),
                    "acoCallCapability": context.callback_capability.get_secret_value()
                    if context.callback_capability
                    else "",
                    "acoObjective": objective.objective,
                    "acoAllowedTools": ",".join(allowed_tools),
                },
            },
        }
        return self._result(await self._request(context, "POST", "/call/web", payload, material=objective))

    async def get_call(self, context: CallContext, reference: ProviderReference) -> ProviderCallRef:
        self._scope(context)
        if reference.provider != "vapi" or reference.connection_id != self.connection_id:
            raise refused()
        try:
            native = UUID(reference.external_id)
        except ValueError as exc:
            raise refused() from exc
        if context.provider_call_id is not None and reference.external_id != context.provider_call_id:
            raise refused()
        await self.authority(context, "get_call", reference)
        call = await self._request(context, "GET", "/call/" + str(native), material=reference)
        if call.id != native:
            raise refused()
        return self._result(call)

    async def verify_webhook(
        self, context: CallContext, raw_body: bytes, headers: Mapping[str, str]
    ) -> VerifiedVoiceWebhook:
        self._scope(context, terminal_verification=self.binding.evidence_mode == "LIVE")
        normalized = {k.lower(): v for k, v in headers.items()}
        timestamp = normalized.get("x-timestamp", "")
        try:
            if len(raw_body) > 65536 or not timestamp.isdigit() or len(timestamp) > 12:
                raise ValueError("Invalid callback bounds")
            if abs(self.clock.now().timestamp() - int(timestamp)) > 60:
                raise ValueError("Stale callback")
            expected = hmac.new(
                self.binding.webhook_secret.get_secret_value().encode(),
                timestamp.encode() + b"." + raw_body,
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(expected, normalized.get("x-signature", "")):
                raise ValueError("Invalid signature")
            value = json.loads(raw_body)
            message = value["message"]
            native = message["call"]["id"]
            if str(UUID(native)) != context.provider_call_id:
                raise ValueError("Foreign call")
            if message["call"].get("assistantId") != str(self.binding.assistant_id):
                raise ValueError("Foreign assistant")
            kind = message["type"]
            if self.binding.evidence_mode == "LIVE" and (
                any(
                    key in message or key in message["call"]
                    for key in ("transcript", "messages", "recordingUrl", "artifact", "toolCallList")
                    if key != "toolCallList" or kind != "tool-calls"
                )
                or kind == "end-of-call-report"
            ):
                raise ValueError("Capture-bearing or mixed event")
            digest = hashlib.sha256(raw_body).hexdigest()
            events = []
            if kind == "tool-calls":
                items = message["toolCallList"]
                if not isinstance(items, list) or not 1 <= len(items) <= 4:
                    raise ValueError("Invalid tool count")
                for item in items:
                    events.append(
                        VoiceEvent(
                            provider_call_id=native,
                            event_key="tool:" + item["id"],
                            kind="tool.requested",
                            invocation_id=item["id"],
                            tool=item["function"]["name"],
                            arguments=item["function"]["arguments"],
                        )
                    )
            elif kind == "status-update":
                status = {
                    "queued": "call.started",
                    "ringing": "call.started",
                    "in-progress": "call.connected",
                    "ended": "call.completed",
                }.get(message["status"])
                if status is None:
                    raise ValueError("Unknown status")
                events.append(
                    VoiceEvent.model_validate(dict(provider_call_id=native, event_key=digest, kind=status))
                )
            elif kind == "end-of-call-report":
                # Discard transcript/messages/recording URLs; they never enter canonical evidence.
                events.append(VoiceEvent(provider_call_id=native, event_key=digest, kind="call.completed"))
            else:
                raise ValueError("Unsupported or capture-bearing event")
        except (ValueError, KeyError, TypeError, ValidationError) as exc:
            raise refused() from exc
        verified = VerifiedVoiceWebhook(
            session_id=context.session_id, raw_sha256=digest, events=tuple(events)
        )
        await self.authority(
            context, "verify_webhook", verified if self.binding.evidence_mode == "LIVE" else digest
        )
        return verified

    def normalize_call_event(self, verified: VerifiedVoiceWebhook) -> tuple[VoiceEvent, ...]:
        return verified.events

    async def get_transcript(self, context: CallContext, reference: ProviderReference) -> None:
        raise refused(ProviderErrorCode.UNSUPPORTED_CAPABILITY)

    async def get_recording_if_permitted(self, context: CallContext, reference: ProviderReference) -> None:
        raise refused(ProviderErrorCode.UNSUPPORTED_CAPABILITY)

    async def cancel_call_if_supported(self, context: CallContext, reference: ProviderReference) -> None:
        raise refused(ProviderErrorCode.UNSUPPORTED_CAPABILITY)

    async def describe_capabilities(self) -> ProviderCapabilities:
        # Implemented binding support only: current profile/grant/action/kill and
        # retained native capabilities are independently required by each operation.
        features = {"get_call", "capture_disabled"}
        if self.binding.evidence_mode == "MOCKED_HTTP":
            features |= {"mocked_browser_start", "configured_hmac_callbacks"}
        else:
            features |= {"guarded_live_browser_start", "guarded_live_hmac_callbacks"}
            if self.binding.control_api_key is not None and self.binding.control_hosts:
                features.add("retained_capability_safety_stop")
        return ProviderCapabilities(
            features=frozenset(features),
            limits={"max_duration_seconds": self.binding.max_duration_seconds, "max_tool_events": 4},
        )
