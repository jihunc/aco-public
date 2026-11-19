"""Canonical vendor-neutral values. Financial values never pass through float."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class Role(StrEnum):
    OWNER = "owner"
    FINANCE_MANAGER = "finance_manager"
    OPERATOR = "operator"
    VIEWER = "viewer"


class Money(StrictModel):
    minor_units: int
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    exponent: Annotated[int, Field(ge=0, le=6)]


class SourceStamp(StrictModel):
    connection_id: UUID
    external_id: Annotated[str, Field(min_length=1)]
    observed_at: AwareDatetime
    source_at: AwareDatetime | None
    revision: str | None
    synthetic: bool

    @field_validator("observed_at", "source_at")
    @classmethod
    def utc(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None


class ProviderReference(StrictModel):
    provider: str
    connection_id: UUID
    external_id: str


class ProviderCapabilities(StrictModel):
    features: frozenset[str] = frozenset()
    limits: dict[str, int] = Field(default_factory=dict)


class ProviderErrorCode(StrEnum):
    AUTH_EXPIRED = "AUTH_EXPIRED"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    RATE_LIMITED = "RATE_LIMITED"
    TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    UNKNOWN_SIDE_EFFECT = "UNKNOWN_SIDE_EFFECT"


class ProviderFailure(StrictModel):
    code: ProviderErrorCode
    retryable: bool
    may_have_side_effect: bool
    request_id: str | None = None
    retry_after: timedelta | None = None

    @model_validator(mode="after")
    def no_blind_retry(self) -> "ProviderFailure":
        if self.may_have_side_effect and self.retryable:
            raise ValueError("Possible effects require reconciliation, never blind retry")
        if self.code == ProviderErrorCode.UNKNOWN_SIDE_EFFECT and not self.may_have_side_effect:
            raise ValueError("Unknown outcome must retain possible side effect")
        return self


class CostQuality(StrEnum):
    PROVIDER_REPORTED = "PROVIDER_REPORTED"
    RATE_ESTIMATE = "RATE_ESTIMATE"
    UNKNOWN = "UNKNOWN"


def validate_charge(value: Decimal | None) -> Decimal | None:
    if value is not None:
        with localcontext() as context:
            context.prec = 80
            if (
                not value.is_finite()
                or value < 0
                or value >= Decimal("1e26")
                or value != value.quantize(Decimal("1e-12"))
            ):
                raise ValueError("Charge requires a finite nonnegative exact NUMERIC(38,12) value")
    return value


class ProviderCost(StrictModel):
    amount: Decimal | None
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    quality: CostQuality
    rate_snapshot_id: UUID | None = None

    @field_validator("amount")
    @classmethod
    def exact_charge(cls, value: Decimal | None) -> Decimal | None:
        return validate_charge(value)

    @field_validator("currency")
    @classmethod
    def valid_currency(cls, value: str) -> str:
        from iso4217 import Currency

        Currency(value)
        return value

    @model_validator(mode="after")
    def unknown_is_not_zero(self) -> "ProviderCost":
        if self.quality == CostQuality.UNKNOWN and self.amount is not None:
            raise ValueError("Unknown charge has no invented amount")
        if self.quality != CostQuality.UNKNOWN and self.amount is None:
            raise ValueError("Known charge requires amount")
        return self


class ActionStatus(StrEnum):
    PROPOSED = "proposed"
    VALIDATED = "validated"
    AWAITING_APPROVAL = "awaiting_approval"
    QUEUED = "queued"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN_OUTCOME = "unknown_outcome"
    CANCELLED = "cancelled"
    DEAD_LETTER = "dead_letter"


class ActionResult(StrictModel):
    action_id: UUID
    status: ActionStatus
    changed_ids: tuple[UUID, ...] = ()
    provider_ref: ProviderReference | None = None
    error_code: str | None = None
