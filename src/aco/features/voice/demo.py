"""Explicit server-only offline provider simulator; no audio, recipient or network transport.

The contained local runtime must opt in and inject delivery through its real callback
route. Browser clients receive neither provider credentials nor callback capabilities.
"""

import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable
from datetime import timedelta
from uuid import UUID, uuid5

import httpx
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aco.core.context import TenantContext
from aco.features.identity.models import now
from aco.features.voice.dto import VoiceSessionView
from aco.features.voice.runtime import VoiceRuntime
from aco.features.voice.service import VoiceService
from aco.features.voice.setup import connection_key
from aco.integrations.vapi.settings import VapiBinding

CallbackSender = Callable[[bytes, dict[str, str]], Awaitable[None]]


class _SimulatorRuntime(VoiceRuntime):
    driver: "OfflineVoiceDemo"

    async def start(self, ctx: TenantContext, key: UUID, expected_version: int) -> VoiceSessionView:
        view = await super().start(ctx, key, expected_version)
        if view.state in {"active", "connected"}:
            await self.driver.deliver(view)
            async with self.sessions.begin() as db:
                return await db.run_sync(lambda store: VoiceService(store, self).get(ctx, key))
        return view


class OfflineVoiceDemo:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        capability_secret: SecretStr,
        callback_sender: CallbackSender,
        enabled: bool = False,
        environment: str = "local",
    ):
        self.secret, self.callback_sender = capability_secret, callback_sender
        self.requests: dict[UUID, dict[str, object]] = {}
        self.runtime = _SimulatorRuntime(
            sessions,
            self.binding,
            capability_secret=capability_secret,
            transport_factory=lambda: httpx.MockTransport(self.respond),
            enabled=enabled,
            setup_enabled=enabled,
            environment=environment,
        )
        self.runtime.driver = self
        from aco.integrations.fake.voice_artifacts import SyntheticVoiceArtifacts

        self.runtime.capture_port = SyntheticVoiceArtifacts()

    def binding(self, tenant: UUID, connection: UUID) -> VapiBinding:
        return synthetic_binding(self.secret, tenant, connection)

    def respond(self, request: httpx.Request) -> httpx.Response:
        """Fixed provider fixture consumed by the real HTTPX adapter, never production ingress."""
        if request.url.host != "api.vapi.ai":
            raise httpx.ConnectError("Unexpected offline provider host", request=request)
        if request.method == "GET" and request.url.path.startswith("/assistant/"):
            assistant = UUID(request.url.path.rsplit("/", 1)[1])
            return httpx.Response(
                200,
                json={
                    "id": str(assistant),
                    "server": {"credentialId": str(uuid5(assistant, "configured-hmac-v1"))},
                    "artifactPlan": {
                        "recordingEnabled": False,
                        "loggingEnabled": False,
                        "videoRecordingEnabled": False,
                        "transcriptPlan": {"enabled": False},
                    },
                },
            )
        if request.method == "POST" and request.url.path == "/call/web":
            body = json.loads(request.content)
            session = UUID(body["assistantOverrides"]["variableValues"]["acoSessionId"])
            self.requests[session] = body
            return httpx.Response(
                201,
                json={
                    "id": str(uuid5(session, "synthetic-web-call")),
                    "type": "webCall",
                    "assistantId": body["assistantId"],
                    "status": "queued",
                },
            )
        return httpx.Response(404, json={"error": "unsupported offline operation"})

    async def deliver(self, view: VoiceSessionView) -> None:
        request = self.requests.get(view.id)
        if request is None:
            raise HTTPException(503, "VOICE_SIMULATOR_REQUEST_UNAVAILABLE")
        # The simulator uses only the capability actually delivered in the provider POST.
        overrides = request["assistantOverrides"]
        assert isinstance(overrides, dict)
        variables = overrides["variableValues"]
        assert isinstance(variables, dict)
        token = variables["acoCallCapability"]
        # Resolve exact persisted tenant from the signed capability, never from browser input.
        claims = self.runtime.signer.loads(str(token))
        tenant = UUID(claims["tenant_id"])
        binding = self.runtime.binding(tenant, view.connection_id)
        call = {
            "id": view.provider_call_id,
            "assistantId": str(binding.assistant_id),
            "assistantOverrides": {"variableValues": {"acoCallCapability": token}},
        }

        async def send(message: dict[str, object]) -> None:
            # Each real callback route independently revalidates current authority/HMAC/capability.
            raw = json.dumps({"message": message}, separators=(",", ":")).encode()
            stamp = str(int(now().timestamp()))
            signature = hmac.new(
                binding.webhook_secret.get_secret_value().encode(),
                stamp.encode() + b"." + raw,
                hashlib.sha256,
            ).hexdigest()
            await self.callback_sender(
                raw, {"content-type": "application/json", "x-timestamp": stamp, "x-signature": signature}
            )

        for index, invoice in enumerate(view.invoice_ids):
            await send(
                {
                    "type": "tool-calls",
                    "call": call,
                    "toolCallList": [
                        {
                            "id": f"synthetic-read-{index}",
                            "type": "function",
                            "function": {"name": "get_invoice", "arguments": {"invoice_id": str(invoice)}},
                        }
                    ],
                }
            )
        await send(
            {
                "type": "tool-calls",
                "call": call,
                "toolCallList": [
                    {
                        "id": "synthetic-task-1",
                        "type": "function",
                        "function": {
                            "name": "create_collection_task",
                            "arguments": {
                                "invoice_id": str(view.invoice_ids[0]),
                                "title": "Review synthetic voice follow-up",
                            },
                        },
                    }
                ],
            }
        )
        if view.demo_scenario == "business_facts":
            invoice_id = str(view.invoice_ids[0])
            due_date = (view.source_scope[0].captured_at + timedelta(days=7)).date().isoformat()
            # Fixed synthetic operational facts; no utterance/transcript or browser text.
            commands: list[tuple[str, dict[str, object]]] = [
                (
                    "upsert_case",
                    {
                        "invoice_id": invoice_id,
                        "case_facts": {
                            "invoice_ids": [invoice_id],
                            "issue_type": "wrong_amount",
                            "claim_reference": "synthetic-voice-rate-" + str(view.id),
                        },
                    },
                ),
                (
                    "record_promise",
                    {
                        "invoice_id": invoice_id,
                        "promise_facts": {
                            "invoice_ids": [invoice_id],
                            "amount": {"minor_units": 4000, "currency": "AUD", "exponent": 2},
                            "due_date": due_date,
                            "timezone": "UTC",
                        },
                    },
                ),
                (
                    "request_collection_hold",
                    {
                        "invoice_id": invoice_id,
                        "hold_facts": {"invoice_ids": [invoice_id], "reason_code": "customer_contact_stop"},
                    },
                ),
            ]
            for index, (tool, arguments) in enumerate(commands):
                await send(
                    {
                        "type": "tool-calls",
                        "call": call,
                        "toolCallList": [
                            {
                                "id": f"synthetic-business-{index}",
                                "type": "function",
                                "function": {"name": tool, "arguments": arguments},
                            }
                        ],
                    }
                )
        await send({"type": "end-of-call-report", "call": call})
        self.requests.pop(view.id, None)


def synthetic_binding(secret: SecretStr, tenant: UUID, connection: UUID) -> VapiBinding:
    if connection != connection_key(tenant):
        raise LookupError("Only the explicitly synthetic connection is configured")
    assistant = uuid5(tenant, "aco.synthetic.voice.assistant.v1")
    signature_secret = hmac.new(
        secret.get_secret_value().encode(), str(connection).encode(), hashlib.sha256
    ).hexdigest()
    return VapiBinding(
        tenant_id=tenant,
        connection_id=connection,
        assistant_id=assistant,
        api_key=SecretStr("synthetic-offline-not-provider-credentials"),
        webhook_secret=SecretStr(signature_secret),
        credential_revision="synthetic-offline-v1",
        server_credential_id=uuid5(assistant, "configured-hmac-v1"),
        evidence_mode="MOCKED_HTTP",
    )


def preparation_runtime(
    sessions: async_sessionmaker[AsyncSession], *, capability_secret: SecretStr | None, environment: str
) -> VoiceRuntime:
    """Same explicit synthetic binding as the API; no simulator delivery or wire authority."""
    if capability_secret is None or environment not in {"local", "demo"}:
        raise ValueError("SYNTHETIC_VOICE_PREPARATION_CONFIGURATION_REQUIRED")

    def refuse_wire(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Preparation runtime cannot execute provider requests", request=request)

    return VoiceRuntime(
        sessions,
        lambda tenant, connection: synthetic_binding(capability_secret, tenant, connection),
        capability_secret=capability_secret,
        transport_factory=lambda: httpx.MockTransport(refuse_wire),
        enabled=True,
        environment=environment,
    )
