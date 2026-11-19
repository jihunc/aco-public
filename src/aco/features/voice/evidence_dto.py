"""Signed operational facts are explicit source references, never captured utterances."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from aco.core.types import StrictModel


class VoiceOperationalEvidence(StrictModel):
    kind: Literal["voice_operational"] = "voice_operational"
    voice_evidence_id: Annotated[UUID, Field(strict=False)]
    facts_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def excerpt(self) -> str:
        return f"Operational business facts {self.voice_evidence_id}; not a transcript or quotation"

    @property
    def offset(self) -> int:
        return 0


class VoiceEvidenceView(StrictModel):
    id: UUID
    session_id: UUID
    invocation_id: UUID
    provider_call_id: str
    native_invocation_id: str
    fact_kind: Literal["case", "promise", "contact_stop"]
    facts_json: str
    facts_hash: str
    observed_at: datetime
    source_state: Literal["VOICE_OPERATIONAL_METADATA"] = "VOICE_OPERATIONAL_METADATA"
    synthetic: bool
    evidence_mode: Literal["MOCKED_HTTP", "LIVE"] = "MOCKED_HTTP"
