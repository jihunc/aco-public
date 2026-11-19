"""Explicit synthetic participant decisions; these DTOs never authorize real capture."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from aco.core.types import StrictModel

JsonUUID = Annotated[UUID, Field(strict=False)]


class CaptureScopes(StrictModel):
    audio_capture: bool = False
    transcript: bool = False
    private_storage: bool = False
    playback: bool = False
    provider_retention: bool = False


class RecordVoiceConsent(StrictModel):
    request_id: JsonUUID
    customer_id: JsonUUID
    voice_connection_id: JsonUUID
    storage_connection_id: JsonUUID | None = None
    decision: Literal["GRANTED", "DECLINED"]
    scopes: CaptureScopes
    subject: Literal["SYNTHETIC_PARTICIPANT"] = "SYNTHETIC_PARTICIPANT"
    method: Literal["EXPLICIT_SYNTHETIC_REVIEW"] = "EXPLICIT_SYNTHETIC_REVIEW"
    purpose: str = Field(min_length=10, max_length=500)
    jurisdiction: str = Field(min_length=2, max_length=100)
    notice: str = Field(min_length=10, max_length=1000)
    evidence: str = Field(min_length=10, max_length=1000)
    expires_at: AwareDatetime = Field(strict=False)

    @model_validator(mode="after")
    def independent_scope(self) -> "RecordVoiceConsent":
        selected = self.scopes
        if self.decision == "DECLINED" and any(selected.model_dump().values()):
            raise ValueError("Declined capture cannot be replaced with a hidden transcript")
        if (selected.audio_capture or selected.transcript) and (
            not selected.private_storage
            or not selected.provider_retention
            or self.storage_connection_id is None
        ):
            raise ValueError("Capture requires separate private storage and provider retention permission")
        if selected.playback and not selected.audio_capture:
            raise ValueError("Playback requires separately permitted recording")
        return self


class RevokeVoiceConsent(StrictModel):
    request_id: JsonUUID
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=10, max_length=1000)


class VoiceConsentView(StrictModel):
    id: UUID
    version: int
    command: RecordVoiceConsent
    recorded_by: UUID
    recorded_at: datetime
    material_hash: str
    revoked_at: datetime | None
    revocation_reason: str | None
    active: bool
    reasons: list[str]
    retention_days: Literal[30] = 30
    synthetic_only: Literal[True] = True


class ConsentSourceView(StrictModel):
    id: UUID
    label: str


class VoiceConsentPage(StrictModel):
    can_prepare_storage: bool = False
    storage_setup_reasons: list[str] = Field(default_factory=list)
    items: list[VoiceConsentView]
    can_record: bool
    reasons: list[str]
    voice_connections: list[ConsentSourceView]
    storage_connections: list[ConsentSourceView]
    synthetic_only: Literal[True] = True


class PrepareConsentStorage(StrictModel):
    request_id: JsonUUID
    evidence: str = Field(min_length=10, max_length=1000)
