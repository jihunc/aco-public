"""Explicit operator-reported durations; no inferred or automatically measured time."""

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from aco.core.types import StrictModel
from aco.features.cases.dto import WireModel


class RecordHandlingTime(WireModel):
    request_id: UUID
    history_id: UUID
    expected_case_version: int = Field(ge=1)
    minutes: Decimal = Field(ge=0, le=1440, max_digits=8, decimal_places=2)
    evidence: str = Field(min_length=10, max_length=1000)
    supersedes_id: UUID | None = None

    @field_validator("minutes", mode="before")
    @classmethod
    def exact_minutes(cls, value: object) -> Decimal:
        if not isinstance(value, (str, Decimal)):
            raise ValueError("Use an exact decimal string for reported minutes")
        try:
            result = Decimal(value)
        except InvalidOperation as error:
            raise ValueError("Invalid reported minutes") from error
        if not result.is_finite():
            raise ValueError("Reported minutes must be finite")
        return result


class HandlingTimeView(StrictModel):
    id: UUID
    case_id: UUID
    history_id: UUID
    actor_id: UUID
    minutes: Decimal
    basis: Literal["MANUAL_REPORTED"] = "MANUAL_REPORTED"
    evidence: str
    supersedes_id: UUID | None
    version: int
    created_at: datetime
    audit_id: UUID


class HandlingTimePage(StrictModel):
    case_id: UUID
    expected_case_version: int
    can_record_history_ids: list[UUID]
    entries: list[HandlingTimeView]
    current_entry_ids: list[UUID]
    caveat: str = (
        "Human-reported active time for a recorded event. Do not count the same work in multiple entries."
    )
