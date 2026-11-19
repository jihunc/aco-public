"""Trusted operator review of ONE canonical tenant ceiling; no HTTP or grant authority.

Provider installers cannot widen or erase this envelope. Every actual provider
request still requires its own current grant, campaign allocation and scope limits.
"""

from decimal import Decimal, localcontext
from uuid import UUID

from fastapi import HTTPException
from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.core.live import DELIVERY_LIVE_AUTHORIZATION
from aco.core.types import StrictModel
from aco.features.identity.models import AuditEvent, Tenant
from aco.features.identity.service import IdentityService, Permission
from aco.features.usage.dto import Amount, Units
from aco.features.usage.models import (
    BudgetLimit,
    BudgetReservation,
    BudgetScope,
    ReservationOutcome,
    ReservationScope,
)


class CompleteLiveUnits(Units):
    requests: int = Field(ge=1, le=100)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_tokens: int = Field(ge=0)
    messages: int = Field(ge=0, le=20)
    duration_seconds: Amount = Field(le=900)


class ReviewLiveTenantBudget(StrictModel):
    authorization_ref: str
    expected_limit_id: UUID | None
    expected_version: int | None = Field(ge=1)
    amount: Amount = Field(gt=0, le=20)
    requests: int = Field(ge=1, le=100)
    units: CompleteLiveUnits
    review_reason: str = Field(min_length=10, max_length=1000)

    @model_validator(mode="after")
    def exact_review(self) -> "ReviewLiveTenantBudget":
        if (self.expected_limit_id is None) != (self.expected_version is None):
            raise ValueError("Capture both prior limit identity and version")
        if self.units.requests != self.requests or not self.review_reason.strip():
            raise ValueError("Explicit matching request ceiling and review required")
        return self


def review_live_tenant_budget(
    db: Session, ctx: TenantContext, command: ReviewLiveTenantBudget
) -> BudgetLimit:
    if not isinstance(ctx, TenantContext):
        raise HTTPException(403, "HUMAN_CONTEXT_REQUIRED")
    command = ReviewLiveTenantBudget.model_validate_json(command.model_dump_json())
    tenant = db.get(Tenant, ctx.tenant_id, with_for_update=True, populate_existing=True)
    IdentityService(db).authorize(ctx, Permission.SETTINGS)
    if tenant is None or not tenant.is_demo or command.authorization_ref != DELIVERY_LIVE_AUTHORIZATION:
        raise HTTPException(403, "CURRENT_TENANT_LIVE_REVIEW_REQUIRED")
    scopes = list(
        db.scalars(
            select(BudgetScope).where(
                BudgetScope.tenant_id == ctx.tenant_id,
                BudgetScope.dimension == "tenant",
                BudgetScope.synthetic.is_(False),
                BudgetScope.provider_connection_id.is_(None),
            )
        )
    )
    if len(scopes) > 1:
        raise HTTPException(409, "TENANT_LIMIT_AMBIGUOUS")
    scope = scopes[0] if scopes else None
    limit = (
        db.scalar(
            select(BudgetLimit)
            .where(
                BudgetLimit.scope_id == scope.id,
                BudgetLimit.currency == "USD",
                BudgetLimit.state == "BOUNDED_LIVE_TEST",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if scope
        else None
    )
    if (limit is None and command.expected_limit_id is not None) or (
        limit is not None
        and (limit.id != command.expected_limit_id or limit.version != command.expected_version)
    ):
        raise HTTPException(409, "TENANT_LIMIT_VERSION_CONFLICT")
    exposure = Decimal(0)
    used_requests = 0
    used_units = {key: Decimal(0) for key in Units.model_fields}
    if scope is not None:
        rows = list(
            db.scalars(
                select(BudgetReservation)
                .join(ReservationScope, ReservationScope.reservation_id == BudgetReservation.id)
                .where(ReservationScope.scope_id == scope.id, BudgetReservation.status != "BLOCKED")
                .execution_options(populate_existing=True)
            )
        )
        with localcontext() as precision:
            precision.prec = 80
            for row in rows:
                if row.currency == "USD":
                    exposure += row.retained_amount + row.spent_amount
                if row.status == "NO_EFFECT":
                    continue
                used_requests += int(str(row.request["requests"]))
                final = db.scalar(
                    select(ReservationOutcome)
                    .where(ReservationOutcome.reservation_id == row.id)
                    .order_by(ReservationOutcome.created_at.desc())
                    .limit(1)
                )
                units = (
                    final.payload.get("units") if final and row.status == "VERIFIED" else None
                ) or row.request["units"]
                if not isinstance(units, dict):
                    raise HTTPException(409, "TENANT_EXPOSURE_UNAVAILABLE")
                for key in used_units:
                    used_units[key] += Decimal(str(units[key]))
    caps = command.units.model_dump(mode="json")
    if (
        command.amount < exposure
        or command.requests < used_requests
        or any(Decimal(str(caps[key])) < used for key, used in used_units.items())
    ):
        raise HTTPException(409, "TENANT_LIMIT_BELOW_EXPOSURE")
    prior = (
        None
        if limit is None
        else {
            "limit_id": str(limit.id),
            "version": limit.version,
            "amount": str(limit.amount),
            "requests": limit.requests,
            "unit_caps": limit.unit_caps,
        }
    )
    if scope is None:
        scope = BudgetScope(
            tenant_id=ctx.tenant_id,
            provider_connection_id=None,
            dimension="tenant",
            synthetic=False,
            creator_id=ctx.actor_id,
        )
        db.add(scope)
        db.flush()
    if limit is None:
        limit = BudgetLimit(
            tenant_id=ctx.tenant_id,
            scope_id=scope.id,
            currency="USD",
            state="BOUNDED_LIVE_TEST",
            amount=command.amount,
            requests=command.requests,
            unit_caps=caps,
        )
        db.add(limit)
    else:
        limit.amount, limit.requests, limit.unit_caps = command.amount, command.requests, caps
        limit.version += 1
    db.flush()
    db.add(
        AuditEvent(
            tenant_id=ctx.tenant_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.correlation_id,
            action="budget.live_tenant_reviewed",
            target_id=limit.id,
            details={
                "command": command.model_dump(mode="json"),
                "prior": prior,
                "version": limit.version,
                "exposure": {
                    "amount": str(exposure),
                    "requests": used_requests,
                    "units": {key: str(value) for key, value in used_units.items()},
                },
            },
        )
    )
    db.flush()
    return limit
