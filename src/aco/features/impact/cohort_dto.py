"""Public cohort evidence; monetary minor units are exact decimal strings."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field

from aco.core.types import StrictModel


class SeedCohorts(StrictModel):
    request_id: UUID
    evidence: str = Field(min_length=10, max_length=1000)


class RefreshCohorts(StrictModel):
    request_id: UUID
    expected_result_ids: list[UUID] = Field(min_length=2, max_length=2)
    evidence: str = Field(min_length=10, max_length=1000)


class CohortMemberView(StrictModel):
    invoice_id: UUID
    original_due_date: date
    issue_date: date
    opening_minor: str | None
    original_minor: str
    source_watermark: str
    opening_source_id: UUID | None
    eligibility: str


class CohortResultView(StrictModel):
    id: UUID
    version: int
    prior_result_id: UUID | None
    label: Literal["BASELINE_SYNTHETIC", "AFTER_SYNTHETIC", "RESTATED", "INCOMPLETE", "OBSERVED"]
    status: Literal["COMPLETE", "INCOMPLETE"]
    as_of_at: datetime
    last_synced_at: datetime
    source_watermark: str
    source_status: Literal["VERIFIED_AT_AS_OF"] = "VERIFIED_AT_AS_OF"
    freshness: Literal["FROZEN_SNAPSHOT"] = "FROZEN_SNAPSHOT"
    reason: str
    eligible_count: int
    excluded_count: int
    opening_minor: str | None
    raw_net_minor: str | None
    recovery_numerator_minor: str | None
    refunds_minor: str
    reversals_minor: str
    recovery_percent: Decimal | None
    paid_in_full_count: int | None
    paid_in_full_percent: Decimal | None
    days_to_collect: Decimal | None
    days_beyond_terms: Decimal | None
    issues: list[str]
    adjustment_ids: list[UUID]
    observation_ids: list[UUID]


class CohortView(StrictModel):
    id: UUID
    side: Literal["BASELINE_SYNTHETIC", "AFTER_SYNTHETIC"]
    period_start: date
    period_end: date
    timezone: str
    currency: str
    exponent: int
    credit_terms_days: int
    business_segment: str
    algorithm: Literal["aco_cash_v1"] = "aco_cash_v1"
    precision: Literal["date_only_local_midnight"] = "date_only_local_midnight"
    members: list[CohortMemberView]
    results: list[CohortResultView]


class CohortPairView(StrictModel):
    id: UUID
    source_connection_id: UUID
    synthetic: Literal[True] = True
    label: str = "Illustrative — Synthetic Data"
    comparison_allowed: bool
    unmatched_strata: list[str]
    recovery_change_pp: Decimal | None
    recovery_relative_change_percent: Decimal | None
    paid_in_full_change_pp: Decimal | None
    cohorts: list[CohortView]
    caveat: str = (
        "Fixed synthetic illustration. Differences do not establish that ACO caused better payment outcomes."
    )


class CohortListView(StrictModel):
    tenant_id: UUID
    can_seed: bool
    can_refresh: bool
    unavailable_reason: str | None
    pairs: list[CohortPairView]
