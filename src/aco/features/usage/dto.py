"""Vendor-neutral usage and bounded reservation contracts; no arbitrary metadata."""

from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aco.core.types import ProviderCost, StrictModel, validate_charge

Amount = Annotated[Decimal, Field(ge=0, max_digits=38, decimal_places=12)]
Dimension = Literal["tenant", "provider", "run", "call", "demo_session"]


class Units(StrictModel):
    requests: int = Field(default=1, ge=0)
    # Native total input includes cached input; pricing subtracts cache once.
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    messages: int = Field(default=0, ge=0)
    duration_seconds: Amount = Decimal(0)

    @model_validator(mode="after")
    def nonoverlapping_tokens(self) -> "Units":
        if self.cached_tokens > self.input_tokens:
            raise ValueError("Cached tokens cannot exceed total native input tokens")
        return self


class ProviderUsageInput(StrictModel):
    provider_connection_id: UUID
    operation_key: str = Field(min_length=1, max_length=200)
    component: str = Field(pattern=r"^[a-z][a-z_]{0,63}$")
    operation: str = Field(pattern=r"^[a-z][a-z_]{0,63}$")
    provider_kind: str = Field(pattern=r"^[a-z][a-z_]{0,63}$")
    provider: str = Field(min_length=1, max_length=512)
    synthetic: bool
    cost: ProviderCost
    units: Units = Units()
    native_operation_id: str | None = None
    native_subject: str | None = Field(default=None, min_length=1, max_length=512)
    model: str | None = None
    model_version: str | None = None
    price_source_at: AwareDatetime | None = None
    outcome: str = "VERIFIED"
    reconciliation_key: str = "initial"
    bundle: str | None = None
    included_in: str | None = None


class UsageReservationRequest(StrictModel):
    operation_key: str = Field(min_length=1, max_length=200)
    provider_connection_id: UUID
    scope_ids: tuple[UUID, ...]
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    amount: Amount | None
    requests: int = Field(default=1, ge=0)
    units: Units = Units()

    @field_validator("amount")
    @classmethod
    def exact(cls, value: Decimal | None) -> Decimal | None:
        return validate_charge(value)


class UsageOutcome(StrictModel):
    key: str = Field(min_length=1, max_length=100)
    status: Literal["VERIFIED", "NO_EFFECT", "UNKNOWN_SIDE_EFFECT", "ABANDONED"]
    verified_unused: bool = False
    actual_amount: Amount | None = None
    units: Units | None = None


class CurrencyUsage(StrictModel):
    currency: str
    synthetic: bool
    known_amount: str
    reported_amount: str
    estimated_amount: str
    unknown_count: int
    bundle_issue_count: int
    completeness: Literal["complete", "partial"]
    reserved_amount: str
    spent_amount: str


class ProviderUsageBasis(StrictModel):
    provider_connection_id: UUID
    provider: str
    provider_kind: str
    currency: str
    synthetic: bool
    reported_amount: str
    estimated_amount: str
    unknown_count: int
    bundle_issue_count: int
    completeness: Literal["complete", "partial"]


BundleIssueReason = Literal[
    "PARENT_MISSING",
    "BUNDLE_IDENTITY_CONFLICT",
    "PARENT_AMOUNT_UNKNOWN",
    "COMPONENT_EXCEEDS_PARENT",
    "BUNDLE_COMPONENTS_EXCEED_PARENT",
    "PARENT_OUTSIDE_FILTER",
]


class BundleIssue(StrictModel):
    event_id: UUID
    parent_event_id: UUID | None
    provider_connection_id: UUID
    component: str
    parent_component: str
    currency: str
    parent_currency: str | None
    component_amount: str | None
    parent_amount: str | None
    reason: BundleIssueReason
    outside_filter: bool = False


class UsageFilters(StrictModel):
    provider_connection_id: UUID | None = Field(default=None, strict=False)
    since: AwareDatetime | None = Field(default=None, strict=False)
    until: AwareDatetime | None = Field(default=None, strict=False)

    @model_validator(mode="after")
    def interval(self) -> "UsageFilters":
        if self.since and self.until and self.since >= self.until:
            raise ValueError("since must precede until")
        return self


class LimitView(StrictModel):
    id: UUID
    dimension: Dimension
    currency: str
    amount: str
    state: str


class UsageSummary(StrictModel):
    external_execution: Literal["BLOCKED"] = "BLOCKED"
    currencies: list[CurrencyUsage]
    providers: list[ProviderUsageBasis]
    bundle_issues: list[BundleIssue]
    limits: list[LimitView]
    notices: list[str]
    history_coverage: str = (
        "Earlier list/feed/failed accounting attempts may lack evidence; never inferred free."
    )


class DraftLimit(StrictModel):
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    amount: Amount
    requests: int = Field(ge=0, default=0)

    @field_validator("amount", mode="before", json_schema_input_type=str)
    @classmethod
    def wire_decimal(cls, value: object) -> Decimal:
        if not isinstance(value, (str, Decimal)):
            raise ValueError("Provider charge must be an exact decimal string")
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            raise ValueError("Provider charge must be an exact decimal string") from None
        validate_charge(parsed)
        return parsed

    @field_validator("currency")
    @classmethod
    def iso_currency(cls, value: str) -> str:
        from iso4217 import Currency

        Currency(value)
        return value
