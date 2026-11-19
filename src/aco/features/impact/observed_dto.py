"""Captured observed cohort commands: execution basis never changes source authenticity."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from iso4217 import Currency
from pydantic import AwareDatetime, Field, field_validator, model_validator

from aco.core.types import StrictModel
from aco.features.cases.dto import WireModel
from aco.features.impact.cohort_dto import CohortMemberView, CohortResultView


class ObservedSelection(WireModel):
    connection_id: UUID
    period_start: date = Field(strict=False)
    period_end: date = Field(strict=False)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    credit_terms_days: int = Field(ge=0, le=3650)
    as_of_at: AwareDatetime | None = Field(default=None, strict=False)

    @field_validator("currency")
    @classmethod
    def currency_valid(cls, value: str) -> str:
        Currency(value)
        return value

    @model_validator(mode="after")
    def period(self) -> "ObservedSelection":
        if self.period_end <= self.period_start:
            raise ValueError("Due-date period must be increasing")
        if (self.period_end - self.period_start).days > 3660:
            raise ValueError("At most ten years per explicit population")
        return self


class FreezeObserved(WireModel):
    request_id: UUID
    selection: ObservedSelection
    reviewed_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence: str = Field(min_length=10, max_length=1000)


class ChangeObserved(FreezeObserved):
    expected_definition_id: UUID
    expected_result_id: UUID


class PreviewObservedChange(WireModel):
    selection: ObservedSelection
    expected_definition_id: UUID
    expected_result_id: UUID
    mode: Literal["REFRESH", "RESTATE"]


class ObservedMember(CohortMemberView):
    source_revision_ids: list[UUID]
    current_due_date: date
    business_segment: str | None
    source_change: str | None


class ObservedPreview(StrictModel):
    preview_only: Literal[True] = True
    selection: ObservedSelection
    reviewed_hash: str
    synthetic: bool
    timezone: str
    business_segments: list[str | None]
    members: list[ObservedMember]
    result: CohortResultView
    warnings: list[str]


class ObservedVersion(StrictModel):
    id: UUID
    version: int
    prior_definition_id: UUID | None
    reason: str
    selection: ObservedSelection
    timezone: str
    members: list[ObservedMember]
    results: list[CohortResultView]
    current_source_members: list[ObservedMember]
    warnings: list[str]


class ObservedCohortView(StrictModel):
    id: UUID
    source_connection_id: UUID
    execution_basis: Literal["OBSERVED"] = "OBSERVED"
    synthetic: bool
    label: str
    comparison_allowed: Literal[False] = False
    versions: list[ObservedVersion]
    caveat: str = (
        "Frozen accounting-sourced population. Source authenticity is unchanged; "
        "no causal uplift or matched comparison is inferred."
    )


class ObservedSource(StrictModel):
    connection_id: UUID
    provider: str
    synthetic: bool


class ObservedList(StrictModel):
    tenant_id: UUID
    can_build: bool
    sources: list[ObservedSource]
    cohorts: list[ObservedCohortView]
    as_of_at: datetime
