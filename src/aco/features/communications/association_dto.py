"""Reviewed additive references to one retained original, never sender authority."""

from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from aco.features.cases.dto import CommunicationEvidence
from aco.features.communications.dto import Command


class LinkCommunication(Command):
    customer_id: UUID
    invoice_ids: list[UUID] = Field(min_length=1, max_length=3)
    expected_version: int = Field(ge=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence: CommunicationEvidence

    @field_validator("invoice_ids")
    @classmethod
    def distinct(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("Duplicate invoice scope")
        return value


class CommunicationLinkCandidate(Command):
    invoice_id: UUID
    customer_id: UUID
    accounting_connection_id: UUID
    number: str
    provenance: Literal["origin_thread", "explicit_reference", "contextual_inference"]


class CommunicationLinkReview(Command):
    communication_id: UUID
    communication_version: int
    source_sha256: str | None
    can_link: bool
    reasons: list[str]
    candidates: list[CommunicationLinkCandidate]
    sender_authority: Literal["UNVERIFIED"] = "UNVERIFIED"
