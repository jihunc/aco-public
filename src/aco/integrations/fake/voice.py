"""A second, explicitly fake native-call shape; never a fallback for Vapi failures."""

from collections.abc import Mapping
from uuid import UUID, uuid5

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
    CallContext,
    CallObjective,
    ConsentMode,
    ProviderCallRef,
    VerifiedVoiceWebhook,
    VoiceAuthority,
    VoiceEvent,
    VoiceFailure,
    VoiceTool,
)


def unsupported() -> VoiceFailure:
    return VoiceFailure(
        ProviderFailure(
            code=ProviderErrorCode.UNSUPPORTED_CAPABILITY, retryable=False, may_have_side_effect=False
        )
    )


class FakeVoiceAdapter:
    def __init__(
        self, tenant_id: UUID, connection_id: UUID, *, authority: VoiceAuthority, clock: Clock | None = None
    ):
        self.tenant_id, self.connection_id, self.authority = tenant_id, connection_id, authority
        self.clock = clock or SystemClock()
        self.calls: dict[UUID, tuple[CallObjective, ProviderCallRef]] = {}

    def scope(self, ctx: CallContext) -> None:
        if (
            ctx.tenant_id != self.tenant_id
            or ctx.connection_id != self.connection_id
            or self.clock.now() >= ctx.expires_at
        ):
            raise VoiceFailure(
                ProviderFailure(
                    code=ProviderErrorCode.AUTH_EXPIRED, retryable=False, may_have_side_effect=False
                )
            )

    async def start_call(
        self,
        objective: CallObjective,
        context: CallContext,
        allowed_tools: tuple[VoiceTool, ...],
        consent_mode: ConsentMode,
    ) -> ProviderCallRef:
        self.scope(context)
        await self.authority(context, "start_call", objective)
        prior = self.calls.get(context.session_id)
        if prior:
            if prior[0] != objective:
                raise unsupported()
            return prior[1]
        result = ProviderCallRef(
            reference=ProviderReference(
                provider="fake_voice",
                connection_id=self.connection_id,
                external_id="simulated/" + str(uuid5(context.session_id, "browser")),
            ),
            status="queued",
            evidence_mode="FAKE",
            cost=ProviderCost(amount=None, currency="USD", quality=CostQuality.UNKNOWN),
        )
        self.calls[context.session_id] = (objective, result)
        return result

    async def get_call(self, context: CallContext, reference: ProviderReference) -> ProviderCallRef:
        self.scope(context)
        await self.authority(context, "get_call", reference)
        prior = self.calls.get(context.session_id)
        if prior is None or prior[1].reference != reference:
            raise unsupported()
        return prior[1]

    async def verify_webhook(
        self, context: CallContext, raw_body: bytes, headers: Mapping[str, str]
    ) -> VerifiedVoiceWebhook:
        raise unsupported()

    def normalize_call_event(self, verified: VerifiedVoiceWebhook) -> tuple[VoiceEvent, ...]:
        raise unsupported()

    async def get_transcript(self, context: CallContext, reference: ProviderReference) -> None:
        raise unsupported()

    async def get_recording_if_permitted(self, context: CallContext, reference: ProviderReference) -> None:
        raise unsupported()

    async def cancel_call_if_supported(self, context: CallContext, reference: ProviderReference) -> None:
        raise unsupported()

    async def describe_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            features=frozenset({"fake_browser_start", "get_call", "capture_disabled"})
        )
