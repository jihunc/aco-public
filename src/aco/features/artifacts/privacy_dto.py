"""Explicit human privacy dispositions; no arbitrary raw media or provider URL."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from aco.core.types import StrictModel
from aco.features.artifacts.consent_dto import JsonUUID
from aco.features.artifacts.dto import ArtifactRef


class PrivacyCommand(StrictModel):
    request_id: JsonUUID
    expected_version: int = Field(ge=1)
    kind: Literal[
        "DELETE_REQUEST", "DELETE_APPROVED", "DELETE_DENIED", "QUARANTINE", "VERIFIED_HOLD", "RELEASE_HOLD"
    ]
    parent_id: JsonUUID | None = None
    reason: str = Field(min_length=10, max_length=1000)
    authority: str | None = Field(default=None, min_length=10, max_length=1000)
    evidence: str | None = Field(default=None, min_length=10, max_length=1000)
    review_at: AwareDatetime | None = Field(default=None, strict=False)

    @model_validator(mode="after")
    def explicit_disposition(self) -> "PrivacyCommand":
        if self.kind in {"DELETE_APPROVED", "DELETE_DENIED"} and self.parent_id is None:
            raise ValueError("A deletion decision requires the exact saved request")
        if self.kind in {"VERIFIED_HOLD", "RELEASE_HOLD"} and (
            not self.authority or not self.evidence or self.review_at is None
        ):
            raise ValueError("Legal review requires authority, evidence and the next review date")
        return self


class PrivacyEventView(StrictModel):
    id: UUID
    actor_id: UUID
    command: PrivacyCommand
    created_at: datetime


class ArtifactPrivacyView(StrictModel):
    artifact: ArtifactRef
    version: int
    legal_hold_state: str
    provider_purge_status: Literal["NOT_APPLICABLE", "PENDING", "PURGED", "BLOCKED"]
    provider_purge_receipt: str | None
    purge_due_at: datetime
    purge_status: str
    failure_code: str | None
    can_request_deletion: bool
    can_review: bool
    events: list[PrivacyEventView]
