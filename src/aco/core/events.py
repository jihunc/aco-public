from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator

from aco.core.types import StrictModel


class EventEnvelope(StrictModel):
    id: UUID
    kind: Annotated[str, Field(min_length=1)]
    tenant_id: UUID
    source_key: Annotated[str, Field(min_length=1)]
    occurred_at: AwareDatetime
    correlation_id: UUID
    payload_version: Annotated[int, Field(ge=1)]
    payload: dict[str, object] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)
