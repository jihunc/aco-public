"""Provider-neutral voice boundary. Values alone never grant a call or tool authority."""

from collections.abc import Mapping
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import AwareDatetime, Field, SecretStr, model_validator

from aco.core.types import ProviderCapabilities, ProviderCost, ProviderFailure, ProviderReference, StrictModel

VoiceTool = Literal[
    "get_invoice",
    "get_communication_history",
    "find_existing_cases",
    "get_payment_promises",
    "create_collection_task",
    "upsert_case",
    "record_promise",
    "request_collection_hold",
]
VOICE_TOOLS: tuple[VoiceTool, ...] = (
    "get_invoice",
    "get_communication_history",
    "find_existing_cases",
    "get_payment_promises",
    "create_collection_task",
    "upsert_case",
    "record_promise",
    "request_collection_hold",
)


class CallObjective(StrictModel):
    primary_invoice_id: UUID
    invoice_ids: tuple[UUID, ...] = Field(min_length=1, max_length=3)
    objective: str = Field(min_length=1, max_length=1000)
    source_as_of: AwareDatetime
    restrictions: tuple[str, ...] = ("No financial changes or outreach", "Do not claim uncommitted actions")

    @model_validator(mode="after")
    def exact_scope(self) -> "CallObjective":
        if self.primary_invoice_id not in self.invoice_ids or len(set(self.invoice_ids)) != len(
            self.invoice_ids
        ):
            raise ValueError("Primary invoice must belong to the complete unique scope")
        return self


class ConsentMode(StrictModel):
    # Capture-on requires a separately implemented legal/retention capability, not a caller boolean.
    recording: Literal[False] = False
    transcript: Literal[False] = False
    participant: Literal["SYNTHETIC_OFFLINE", "LIVE_HUMAN_MICROPHONE_CAPTURE_OFF"] = "SYNTHETIC_OFFLINE"


class CallContext(StrictModel):
    tenant_id: UUID
    connection_id: UUID
    session_id: UUID
    actor_id: UUID
    correlation_id: UUID
    expires_at: AwareDatetime
    provider_call_id: str | None = None
    callback_capability: SecretStr | None = None


class VoiceSetupContext(StrictModel):
    tenant_id: UUID
    connection_id: UUID
    actor_id: UUID
    correlation_id: UUID
    expires_at: AwareDatetime
    connection_version: int
    tenant_version: int
    membership_version: int
    actor_version: int
    binding_hash: str


class VoiceSetupAuthority(Protocol):
    async def __call__(self, ctx: VoiceSetupContext, /) -> None: ...


class ProviderCallRef(StrictModel):
    reference: ProviderReference
    status: Literal["queued", "ringing", "in-progress", "ended"]
    synthetic: bool = True
    evidence_mode: Literal["MOCKED_HTTP", "FAKE", "LIVE"]
    browser_join: SecretStr | None = Field(default=None, exclude=True, repr=False)
    control_url: SecretStr | None = Field(default=None, exclude=True, repr=False)
    cost: ProviderCost

    @model_validator(mode="after")
    def exact_transport_provenance(self) -> "ProviderCallRef":
        if self.synthetic == (self.evidence_mode == "LIVE"):
            raise ValueError("LIVE transport must retain real transport provenance")
        if self.evidence_mode != "LIVE" and (self.browser_join or self.control_url):
            raise ValueError("Offline sessions expose no provider join capability")
        return self


class VoiceEvent(StrictModel):
    provider_call_id: str
    event_key: str
    kind: Literal["call.started", "call.connected", "call.completed", "call.failed", "tool.requested"]
    invocation_id: str | None = Field(default=None, max_length=200)
    tool: VoiceTool | None = None
    arguments: dict[str, object] = Field(default_factory=dict)


class VerifiedVoiceWebhook(StrictModel):
    """Transient normalized authenticated values; no raw transcript/audio or vendor URL."""

    session_id: UUID
    raw_sha256: str
    events: tuple[VoiceEvent, ...]
    authentication: Literal["CONFIGURED_HMAC_SHA256_TIMESTAMP"] = "CONFIGURED_HMAC_SHA256_TIMESTAMP"


class VoiceFailure(Exception):
    def __init__(self, failure: ProviderFailure):
        self.failure = failure
        super().__init__(failure.code.value)


class VoiceAuthority(Protocol):
    async def __call__(self, ctx: CallContext, operation: str, material: object, /) -> None: ...


@runtime_checkable
class VoicePort(Protocol):
    connection_id: UUID

    async def start_call(
        self,
        objective: CallObjective,
        context: CallContext,
        allowed_tools: tuple[VoiceTool, ...],
        consent_mode: ConsentMode,
    ) -> ProviderCallRef: ...
    async def get_call(self, context: CallContext, reference: ProviderReference) -> ProviderCallRef: ...
    async def verify_webhook(
        self, context: CallContext, raw_body: bytes, headers: Mapping[str, str]
    ) -> VerifiedVoiceWebhook: ...
    def normalize_call_event(self, verified: VerifiedVoiceWebhook) -> tuple[VoiceEvent, ...]: ...
    async def get_transcript(self, context: CallContext, reference: ProviderReference) -> None: ...
    async def get_recording_if_permitted(
        self, context: CallContext, reference: ProviderReference
    ) -> None: ...
    async def cancel_call_if_supported(self, context: CallContext, reference: ProviderReference) -> None: ...
    async def describe_capabilities(self) -> ProviderCapabilities: ...
