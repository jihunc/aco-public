"""Observe the saved assistant and CustomCredential; never infer privacy/auth from an ID.

Only Vapi's actual bounded GET results are compared. Masked or absent HMAC material
is unverifiable and fails closed. Neither raw document nor secret is returned/stored.
"""

import hashlib
import hmac
import json
from collections.abc import Callable
from uuid import UUID

import httpx

from aco.core.types import ProviderErrorCode, StrictModel
from aco.integrations.ports.voice import VoiceFailure, VoiceSetupAuthority, VoiceSetupContext
from aco.integrations.ports.voice_observer import VoiceRequestObserver
from aco.integrations.vapi.adapter import refused
from aco.integrations.vapi.settings import VapiBinding


class SavedVoiceProfile(StrictModel):
    assistant_attempt_id: UUID
    credential_attempt_id: UUID
    assistant_profile_sha256: str


class VapiSavedProfileProbe:
    def __init__(
        self,
        binding: VapiBinding,
        *,
        observer: VoiceRequestObserver,
        transport: httpx.AsyncBaseTransport,
    ):
        self.binding, self.observer, self.transport = binding, observer, transport

    async def _read(
        self,
        ctx: VoiceSetupContext,
        authority: VoiceSetupAuthority,
        operation: str,
        native: UUID,
        validator: Callable[[dict[str, object]], None],
    ) -> UUID:
        key = await self.observer.started(ctx, operation)
        attempted, failure = False, "NO_EFFECT"
        try:
            await authority(ctx)
            attempted, failure = True, "TEMPORARILY_UNAVAILABLE"
            kind = "assistant" if operation == "probe_assistant" else "credential"
            async with httpx.AsyncClient(
                transport=self.transport, trust_env=False, follow_redirects=False, timeout=10
            ) as client:
                async with client.stream(
                    "GET",
                    f"https://api.vapi.ai/{kind}/{native}",
                    headers={"Authorization": "Bearer " + self.binding.api_key.get_secret_value()},
                ) as response:
                    if response.status_code != 200:
                        raise refused(ProviderErrorCode.TEMPORARILY_UNAVAILABLE)
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 131072:
                            raise ValueError("Oversized saved profile")
                    failure = "VALIDATION_FAILED"
                    value = json.loads(raw)
                    if not isinstance(value, dict):
                        raise ValueError("Invalid saved profile")
                    validator(value)
                    failure = ""
        except VoiceFailure:
            raise
        except (httpx.TransportError, ValueError, TypeError, AttributeError):
            raise refused(ProviderErrorCode.VALIDATION_FAILED) from None
        finally:
            try:
                await self.observer.finished(
                    key,
                    operation,
                    native=str(native) if not failure else None,
                    failure=failure or None,
                )
            except Exception as exc:
                raise refused(ProviderErrorCode.UNKNOWN_SIDE_EFFECT, uncertain=attempted) from exc
        return key

    async def verify(
        self,
        ctx: VoiceSetupContext,
        authority: VoiceSetupAuthority,
        *,
        account_id: str,
        callback_url: str,
        expected_sha256: str,
    ) -> SavedVoiceProfile:
        if (
            self.binding.evidence_mode != "LIVE"
            or self.binding.server_credential_id is None
            or (ctx.tenant_id, ctx.connection_id) != (self.binding.tenant_id, self.binding.connection_id)
        ):
            raise refused(ProviderErrorCode.AUTH_EXPIRED)

        def assistant(value: dict[str, object]) -> None:
            server, plan = value.get("server"), value.get("artifactPlan")
            if not isinstance(server, dict) or not isinstance(plan, dict):
                raise ValueError("Saved privacy/authentication profile required")
            transcript, monitor = plan.get("transcriptPlan"), value.get("monitorPlan")
            digest = hashlib.sha256(
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if (
                value.get("id") != str(self.binding.assistant_id)
                or value.get("orgId") != account_id
                or server.get("url") != callback_url
                or server.get("credentialId") != str(self.binding.server_credential_id)
                or plan.get("recordingEnabled") is not False
                or plan.get("loggingEnabled") is not False
                or plan.get("videoRecordingEnabled") is not False
                or not isinstance(transcript, dict)
                or transcript.get("enabled") is not False
                or not isinstance(monitor, dict)
                or monitor.get("controlAuthenticationEnabled") is not True
                or value.get("maxDurationSeconds") != self.binding.max_duration_seconds
                or not hmac.compare_digest(digest, expected_sha256)
            ):
                raise ValueError("Saved assistant configuration changed")

        def credential(value: dict[str, object]) -> None:
            plan = value.get("authenticationPlan")
            if not isinstance(plan, dict):
                raise ValueError("Saved authentication profile required")
            secret = plan.get("secretKey")
            if (
                value.get("id") != str(self.binding.server_credential_id)
                or value.get("orgId") != account_id
                or value.get("provider") != "custom-credential"
                or plan.get("type") != "hmac"
                or plan.get("algorithm") != "sha256"
                or plan.get("signatureHeader", "x-signature") != "x-signature"
                or plan.get("timestampHeader", "x-timestamp") != "x-timestamp"
                or plan.get("payloadFormat", "{timestamp}.{body}") != "{timestamp}.{body}"
                or plan.get("signatureEncoding", "hex") != "hex"
                or plan.get("includeTimestamp", True) is not True
                or plan.get("secretIsBase64", False) is not False
                or plan.get("signaturePrefix", "") != ""
                or not isinstance(secret, str)
                or not hmac.compare_digest(secret, self.binding.webhook_secret.get_secret_value())
            ):
                raise ValueError("Saved authentication configuration unverifiable")

        first = await self._read(ctx, authority, "probe_assistant", self.binding.assistant_id, assistant)
        second = await self._read(
            ctx, authority, "probe_credential", self.binding.server_credential_id, credential
        )
        return SavedVoiceProfile(
            assistant_attempt_id=first, credential_attempt_id=second, assistant_profile_sha256=expected_sha256
        )
