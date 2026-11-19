"""Neutral permitted voice originals; no permanent URLs or client-supplied media."""

from collections.abc import Awaitable, Callable
from typing import Literal, Protocol
from uuid import UUID

from pydantic import AwareDatetime, Field

from aco.core.types import StrictModel


class VoiceArtifactRequest(StrictModel):
    tenant_id: UUID
    connection_id: UUID
    session_id: UUID
    consent_id: UUID
    provider_call_id: str
    captured_at: AwareDatetime
    kind: Literal["transcript", "recording"]


class VoiceOriginal(StrictModel):
    request: VoiceArtifactRequest
    reference: str
    kind: Literal["transcript", "recording"]
    content_type: str
    body: bytes = Field(repr=False)
    captured_at: AwareDatetime
    synthetic: Literal[True] = True
    evidence_mode: Literal["FAKE"] = "FAKE"


VoiceArtifactAuthority = Callable[[VoiceArtifactRequest], Awaitable[None]]


class VoiceArtifactPort(Protocol):
    async def fetch(
        self, request: VoiceArtifactRequest, authorize: VoiceArtifactAuthority
    ) -> VoiceOriginal: ...
    async def purge(self, request: VoiceArtifactRequest, authorize: VoiceArtifactAuthority) -> str: ...


class TranscriptTurn(StrictModel):
    speaker: str = Field(min_length=1, max_length=100)
    offset_ms: int = Field(ge=0)
    text: str = Field(max_length=10000)


class SyntheticTranscript(StrictModel):
    synthetic: Literal[True]
    evidence_mode: Literal["FAKE"]
    turns: list[TranscriptTurn] = Field(max_length=10000)
