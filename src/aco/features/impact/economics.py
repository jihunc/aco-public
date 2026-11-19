"""Exact noncausal cost/capacity arithmetic; callers supply validated source coverage."""

from decimal import ROUND_DOWN, Decimal, localcontext
from typing import Literal

from aco.core.types import StrictModel


class CostComponents(StrictModel):
    provider: Decimal | None
    provider_quality: Literal["KNOWN", "UNKNOWN"]
    human: Decimal | None
    fixed: Decimal | None
    complete_cost_per_resolution: Decimal | None
    status: Literal["complete", "partial"]
    billable_activation_allowed: Literal[False] = False


class CapacityEstimate(StrictModel):
    estimated_time_saved_minutes: Decimal | None
    estimated_capacity_value: Decimal | None
    estimated_net_benefit: Decimal | None
    incremental_cost_denominator: Decimal | None
    estimated_roi_percent: Decimal | None
    label: Literal["ESTIMATE", "Insufficient data"]
    realized_payroll_savings_claim: Literal[False] = False


def equal_weights(count: int) -> list[Decimal]:
    if count < 1 or count > 10000:
        raise ValueError("Allocation requires 1..10000 unique leaves")
    with localcontext() as precision:
        precision.prec = 80
        share = (Decimal(1) / count).quantize(Decimal("0.000000000001"), rounding=ROUND_DOWN)
        return [share] * (count - 1) + [Decimal(1) - share * (count - 1)]


def cost_components(
    *,
    provider: Decimal | None,
    human_minutes: Decimal | None,
    wage: Decimal | None,
    fixed: Decimal | None,
    resolved: int,
) -> CostComponents:
    with localcontext() as precision:
        precision.prec = 80
        human = human_minutes * wage / 60 if human_minutes is not None and wage is not None else None
        complete = provider is not None and human is not None and fixed is not None and resolved > 0
        total = (
            (provider + human + fixed) / resolved
            if complete and provider is not None and human is not None and fixed is not None
            else None
        )
        return CostComponents(
            provider=provider,
            provider_quality="UNKNOWN" if provider is None else "KNOWN",
            human=human,
            fixed=fixed,
            complete_cost_per_resolution=total,
            status="complete" if complete else "partial",
        )


def estimate_capacity(
    *,
    eligible: int,
    baseline: Decimal | None,
    human_minutes: Decimal | None,
    wage: Decimal | None,
    provider: Decimal | None,
    fixed: Decimal | None,
    approved: bool,
    same_currency: bool,
    human_time_measured: bool,
) -> CapacityEstimate:
    with localcontext() as precision:
        precision.prec = 80
        minutes = (
            baseline * eligible - human_minutes
            if approved and baseline is not None and human_minutes is not None and eligible > 0
            else None
        )
        capacity = minutes * wage / 60 if minutes is not None and wage is not None else None
        cost = (
            provider + fixed
            if approved and same_currency and provider is not None and fixed is not None
            else None
        )
        benefit = capacity - cost if capacity is not None and cost is not None else None
        roi = (
            benefit / cost * 100
            if human_time_measured and benefit is not None and cost is not None and cost > 0
            else None
        )
        return CapacityEstimate(
            estimated_time_saved_minutes=minutes,
            estimated_capacity_value=capacity,
            estimated_net_benefit=benefit,
            incremental_cost_denominator=cost,
            estimated_roi_percent=roi,
            label="ESTIMATE" if roi is not None else "Insufficient data",
        )
