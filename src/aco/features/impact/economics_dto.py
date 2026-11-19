"""Reviewed operating assumptions and canonical allocation evidence, exact decimal wires."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from iso4217 import Currency
from pydantic import AwareDatetime, BeforeValidator, Field, field_validator, model_validator

from aco.core.types import StrictModel
from aco.features.cases.dto import WireModel
from aco.features.impact.dto import ImpactQuery
from aco.features.impact.economics import CapacityEstimate, CostComponents
from aco.features.usage.dto import UsageSummary


def exact_decimal(value: object) -> Decimal:
    if not isinstance(value, (str, Decimal)):
        raise ValueError("Use an exact decimal string")
    try:
        parsed = Decimal(value)
    except ArithmeticError as error:
        raise ValueError("Invalid decimal") from error
    if not parsed.is_finite() or parsed >= Decimal("1e26"):
        raise ValueError("Finite decimal below 10^26 required")
    return parsed


ExactAmount = Annotated[
    Decimal, BeforeValidator(exact_decimal), Field(ge=0, max_digits=38, decimal_places=12)
]


class SaveEconomicsAssumptions(WireModel):
    request_id: UUID
    expected_previous_version_id: UUID | None
    since: AwareDatetime = Field(strict=False)
    until: AwareDatetime = Field(strict=False)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    synthetic: bool
    baseline_minutes_per_issue: ExactAmount
    wage_per_hour: ExactAmount
    fixed_cost: ExactAmount | None
    fixed_cost_evidence: str | None = Field(default=None, min_length=10, max_length=1000)
    evidence: str = Field(min_length=10, max_length=1000)

    @field_validator("currency")
    @classmethod
    def currency_exists(cls, value: str) -> str:
        Currency(value)
        return value

    @model_validator(mode="after")
    def interval(self) -> "SaveEconomicsAssumptions":
        if self.since >= self.until:
            raise ValueError("since must precede until")
        if self.fixed_cost is not None and self.fixed_cost_evidence is None:
            raise ValueError("Explicit fixed-cost allocation evidence required, including zero")
        return self


class ApproveEconomicsAssumptions(WireModel):
    request_id: UUID
    expected_current_approval_id: UUID | None
    reviewed_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence: str = Field(min_length=10, max_length=1000)


class EconomicsAssumptionView(StrictModel):
    id: UUID
    version: int
    previous_version_id: UUID | None
    creator_id: UUID
    created_at: datetime
    reviewed_hash: str
    command: SaveEconomicsAssumptions
    approval_id: UUID | None
    approved_by: UUID | None
    approved_at: datetime | None
    approval_evidence: str | None


class AllocationLeaf(WireModel):
    case_id: UUID | None
    invoice_id: UUID | None
    weight: ExactAmount

    @model_validator(mode="after")
    def valid_leaf(self) -> "AllocationLeaf":
        if self.case_id is not None and self.invoice_id is None:
            raise ValueError("Case share requires its exact invoice leaf")
        if not 0 < self.weight <= 1:
            raise ValueError("Allocation weights must be positive and at most one")
        return self


class SaveCostAllocation(WireModel):
    request_id: UUID
    expected_revision_id: UUID
    reviewed_source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_version_id: UUID | None
    leaves: list[AllocationLeaf] | None = Field(default=None, min_length=1, max_length=1000)
    evidence: str = Field(min_length=10, max_length=1000)

    @model_validator(mode="after")
    def complete_set(self) -> "SaveCostAllocation":
        if self.leaves is not None:
            identities = {(leaf.case_id, leaf.invoice_id) for leaf in self.leaves}
            if len(identities) != len(self.leaves) or sum(leaf.weight for leaf in self.leaves) != 1:
                raise ValueError("One distinct complete leaf set must sum exactly to one")
        return self


class CostAllocationView(StrictModel):
    id: UUID | None
    event_id: UUID
    version: int
    previous_version_id: UUID | None
    basis_revision_id: UUID | None
    basis: Literal["EQUAL_VERIFIED_LEAVES", "OWNER_REVIEWED_WEIGHTS", "UNALLOCATED"]
    leaves: list[AllocationLeaf]
    supported_leaves: list[AllocationLeaf]
    source_hash: str
    source_ids: list[UUID]
    evidence: str | None
    created_at: datetime | None
    current_source_matches: bool


class UsageCostEvidence(StrictModel):
    event_id: UUID
    revision_id: UUID | None
    revision_version: int | None
    provider_connection_id: UUID
    operation_key: str
    component: str
    provider: str
    currency: str | None
    synthetic: bool
    amount: Decimal | None
    quality: str
    additive: bool
    created_at: datetime
    allocation: CostAllocationView


class AllocatedCostRollup(StrictModel):
    target_id: UUID | None
    currency: str | None
    synthetic: bool
    known_amount: Decimal
    unknown_event_count: int
    event_ids: list[UUID]


class EconomicsGroup(StrictModel):
    currency: str
    synthetic: bool
    eligible_case_count: int
    resolved_case_count: int
    case_ids: list[UUID]
    handling_entry_ids: list[UUID]
    human_time_basis: Literal["MANUAL_REPORTED", "UNAVAILABLE"]
    reported_human_minutes: Decimal | None
    measured_human_cost: Decimal | None = None
    estimated_reported_human_cost: Decimal | None
    estimated_cost_per_resolution: Decimal | None
    known_provider_cost: Decimal | None
    provider_basis_complete: bool
    assumptions_id: UUID | None
    costs: CostComponents
    capacity: CapacityEstimate
    reasons: list[str]


class EconomicsView(StrictModel):
    tenant_id: UUID
    since: datetime
    until: datetime
    as_of_at: datetime
    can_manage: bool
    current_approval_id: UUID | None
    latest_assumption_id: UUID | None
    assumptions: list[EconomicsAssumptionView]
    groups: list[EconomicsGroup]
    usage: UsageSummary
    events: list[UsageCostEvidence]
    case_costs: list[AllocatedCostRollup]
    invoice_costs: list[AllocatedCostRollup]
    source_basis: Literal["RECORDED_OPERATING_EVIDENCE"] = "RECORDED_OPERATING_EVIDENCE"
    caveat: str = (
        "Recorded charges and manually reported time are not complete vendor billing, measured labour, "
        "realized payroll savings or causal ROI. No FX conversion. Case and invoice rollups are "
        "two views of the same canonical leaves; never add those views together."
    )


class EconomicsQuery(ImpactQuery):
    pass
