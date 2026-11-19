"""Saved account auth/privacy verification through offline HTTP, not actual account readiness."""

import hashlib
import json
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr

from aco.features.identity.models import now
from aco.integrations.ports.voice import CallContext, VoiceFailure, VoiceSetupContext
from aco.integrations.vapi.settings import VapiBinding


@pytest.mark.parametrize("wrong", ["none", "url", "credential", "capture", "account", "masked_secret"])
async def test_saved_profile_and_hmac_credential_must_be_observed_exactly(wrong: str) -> None:
    from aco.integrations.vapi.profile import VapiSavedProfileProbe

    assistant, credential, tenant, connection, actor = [uuid4() for _ in range(5)]
    binding = VapiBinding(
        tenant_id=tenant,
        connection_id=connection,
        assistant_id=assistant,
        server_credential_id=credential,
        api_key=SecretStr("offline-not-a-key"),
        webhook_secret=SecretStr("offline-webhook-secret"),
        credential_revision="offline-only",
        evidence_mode="LIVE",
    )
    profile = {
        "id": str(assistant),
        "orgId": "offline-account",
        "server": {"url": "https://voice.example.test/api/voice/callback", "credentialId": str(credential)},
        "artifactPlan": {
            "recordingEnabled": False,
            "loggingEnabled": False,
            "videoRecordingEnabled": False,
            "transcriptPlan": {"enabled": False},
        },
        "maxDurationSeconds": 300,
        "monitorPlan": {"controlAuthenticationEnabled": True},
    }
    expected = hashlib.sha256(json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if wrong == "url":
        profile["server"] = {
            "url": "https://foreign.example.test/api/voice/callback",
            "credentialId": str(credential),
        }
    if wrong == "credential":
        profile["server"] = {
            "url": "https://voice.example.test/api/voice/callback",
            "credentialId": str(uuid4()),
        }
    if wrong == "capture":
        profile["artifactPlan"] = {"recordingEnabled": True}
    if wrong == "account":
        profile["orgId"] = "different-account"
    requests: list[str] = []
    attempts: list[UUID] = []
    completions: list[tuple[str, str | None]] = []
    checks: list[str] = []

    class Observer:
        async def started(self, ctx: CallContext | VoiceSetupContext, operation: str) -> UUID:
            key = uuid4()
            attempts.append(key)
            return key

        async def finished(
            self, key: UUID, operation: str, *, native: str | None, failure: str | None
        ) -> None:
            completions.append((operation, failure))

    async def current(ctx: VoiceSetupContext) -> None:
        checks.append("current")

    def transport(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer offline-not-a-key"
        requests.append(request.url.path)
        if request.url.path.startswith("/assistant/"):
            return httpx.Response(200, json=profile)
        return httpx.Response(
            200,
            json={
                "id": str(credential),
                "orgId": "offline-account",
                "provider": "custom-credential",
                "authenticationPlan": {
                    "type": "hmac",
                    "algorithm": "sha256",
                    "secretKey": "***" if wrong == "masked_secret" else "offline-webhook-secret",
                    "signatureHeader": "x-signature",
                    "timestampHeader": "x-timestamp",
                    "payloadFormat": "{timestamp}.{body}",
                    "signatureEncoding": "hex",
                    "includeTimestamp": True,
                    "secretIsBase64": False,
                },
            },
        )

    ctx = VoiceSetupContext(
        tenant_id=tenant,
        connection_id=connection,
        actor_id=actor,
        correlation_id=uuid4(),
        expires_at=now() + timedelta(minutes=1),
        connection_version=1,
        tenant_version=1,
        membership_version=1,
        actor_version=1,
        binding_hash="a" * 64,
    )
    probe = VapiSavedProfileProbe(binding, observer=Observer(), transport=httpx.MockTransport(transport))
    if wrong == "none":
        result = await probe.verify(
            ctx,
            current,
            account_id="offline-account",
            callback_url="https://voice.example.test/api/voice/callback",
            expected_sha256=expected,
        )
        assert result.assistant_attempt_id == attempts[0] and result.credential_attempt_id == attempts[1]
        assert result.assistant_profile_sha256 == expected
        assert requests == [f"/assistant/{assistant}", f"/credential/{credential}"]
        assert completions == [("probe_assistant", None), ("probe_credential", None)]
    else:
        with pytest.raises(VoiceFailure):
            await probe.verify(
                ctx,
                current,
                account_id="offline-account",
                callback_url="https://voice.example.test/api/voice/callback",
                expected_sha256=expected,
            )
        assert completions[-1][1] is not None
    assert len(checks) == len(requests) == len(attempts) == len(completions)
