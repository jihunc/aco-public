"""Artifact application values, with private bytes excluded from representations."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from aco.core.types import StrictModel


class ArtifactWrite(StrictModel):
    artifact_id: UUID | None = None
    source_kind: Literal["synthetic_fixture", "provider_message", "provider_call"]
    source_reference: str = Field(min_length=1, max_length=200)
    content_type: str = Field(min_length=1, max_length=100)
    body: bytes = Field(repr=False)
    capture_class: Literal["synthetic_email", "synthetic_consented_voice", "real"]
    captured_at: AwareDatetime | None = None


class ArtifactRef(StrictModel):
    id: UUID
    source_reference: str
    sha256: str
    content_type: str
    size_bytes: int
    storage_state: str
    failure_code: str | None
    captured_at: datetime
    expires_at: datetime
    retention_policy_id: UUID


class AuthorizedDownload(StrictModel):
    artifact_id: UUID
    body: bytes = Field(repr=False)
    content_type: str
    sha256: str


class DeletionReceipt(StrictModel):
    artifact_id: UUID
    purge_id: UUID
    status: str
    sha256: str
    confirmed_at: datetime | None
    provider_receipt: str | None
    failure_code: str | None
