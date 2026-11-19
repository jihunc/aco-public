"""Incremental read-only reporting; complete cohort and resolution metrics stay unavailable."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from aco.core.types import StrictModel
from aco.features.receivables.queries import MoneyJSON, OperationsView
from aco.features.usage.dto import UsageSummary


class ImpactQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    since: AwareDatetime = Field(strict=False)
    until: AwareDatetime = Field(strict=False)

    @model_validator(mode="after")
    def interval(self) -> "ImpactQuery":
        if self.since >= self.until:
            raise ValueError("since must precede until")
        return self


class CashIssue(StrictModel):
    allocation_id: UUID
    invoice_id: UUID
    reason: str


class CashEvidence(StrictModel):
    allocation_id: UUID
    invoice_id: UUID
    provider_connection_id: UUID
    source_revision: str
    kind: str
    minor_units: str
    effective_at: datetime
    observed_at: datetime
    imported_at: datetime
    parent_id: UUID | None
    included: bool


class CashGroup(StrictModel):
    currency: str
    exponent: int
    synthetic: bool
    cash: MoneyJSON | None
    outflows: MoneyJSON | None
    net_cash: MoneyJSON | None
    allocation_count: int
    non_cash_count: int
    excluded_count: int
    completeness: Literal["partial", "unavailable"]
    freshness: Literal["fresh", "stale", "unknown"]
    last_synced_at: datetime | None
    issues: list[CashIssue]
    evidence: list[CashEvidence]


class ActivityGroup(StrictModel):
    kind: Literal["agent_runs", "scheduled_actions", "cases", "usage_events"]
    synthetic: bool
    count: int
    statuses: dict[str, int]


class ActivityRecord(StrictModel):
    kind: Literal["agent_runs", "scheduled_actions", "cases", "usage_events"]
    id: UUID
    invoice_id: UUID | None
    status: str
    synthetic: bool
    created_at: datetime


class UnavailableMetric(StrictModel):
    metric: str
    reason: str


class ImpactView(StrictModel):
    tenant_id: UUID
    since: datetime
    until: datetime
    as_of_at: datetime
    cash_basis: str = (
        "Known imported verified allocations effective in [since, until), observed and imported "
        "through as_of_at. Cash less linked refunds/reversals; credits/write-offs excluded. "
        "No complete source-history watermark is stored; all cash results are partial. No FX."
    )
    activity_basis: str = (
        "Records created in [since, until), with their current saved statuses. "
        "Activity is not achieved business impact or autonomous resolution."
    )
    usage_basis: str = (
        "Canonical usage identities created in [since, until), using current latest revisions, "
        "not a historical billing snapshot. Selected reservation exposure and spend are current "
        "balances, not additional provider fees."
    )
    source_warnings: list[str]
    operations: OperationsView
    cash: list[CashGroup]
    activity: list[ActivityGroup]
    recent_activity: list[ActivityRecord]
    usage: UsageSummary
    unavailable: list[UnavailableMetric]
