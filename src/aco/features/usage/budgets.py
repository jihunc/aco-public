"""Fail-closed financial bounds. Reservations never grant real execution authority."""

from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import cast
from uuid import UUID

from fastapi import HTTPException
from iso4217 import Currency
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.core.types import validate_charge
from aco.features.identity.models import ProviderConnection, Tenant
from aco.features.identity.service import IdentityService, Permission
from aco.features.usage.dto import Dimension, Units, UsageOutcome, UsageReservationRequest
from aco.features.usage.models import (
    BudgetLimit,
    BudgetReservation,
    BudgetScope,
    ReservationOutcome,
    ReservationScope,
)
from aco.features.usage.service import UsageContext, connection_for

_SEAL = object()


@dataclass(frozen=True)
class SyntheticAuthority:
    tenant_id: UUID
    connection_id: UUID
    seal: object


class BudgetService:
    def __init__(self, db: Session):
        self.db = db

    def synthetic_authority(
        self, ctx: TenantContext, connection_id: UUID, app_env: str
    ) -> SyntheticAuthority:
        tenant = IdentityService(self.db).authorize(ctx, Permission.SETTINGS)
        connection = connection_for(self.db, ctx, connection_id)
        if (
            app_env not in {"local", "demo"}
            or not tenant.is_demo
            or connection.provider not in {"demo", "mock"}
            or connection.status != "READY"
        ):
            raise HTTPException(403, "SYNTHETIC_ONLY")
        return SyntheticAuthority(ctx.tenant_id, connection_id, _SEAL)

    def _authority(
        self, ctx: TenantContext, connection_id: UUID, authority: SyntheticAuthority | None
    ) -> bool:
        connection = connection_for(self.db, ctx, connection_id)
        tenant = self.db.get(Tenant, ctx.tenant_id, populate_existing=True)
        return (
            tenant is not None
            and tenant.is_demo
            and connection.status == "READY"
            and authority is not None
            and authority.seal is _SEAL
            and authority.tenant_id == ctx.tenant_id
            and authority.connection_id == connection_id
            and connection.provider in {"demo", "mock"}
        )

    def create_scope(
        self, ctx: TenantContext, authority: SyntheticAuthority, dimension: Dimension
    ) -> BudgetScope:
        IdentityService(self.db).authorize(ctx, Permission.SETTINGS)
        if not self._authority(ctx, authority.connection_id, authority):
            raise HTTPException(403, "SYNTHETIC_ONLY")
        connection_id = None if dimension == "tenant" else authority.connection_id
        if dimension in {"tenant", "provider"}:
            query = select(BudgetScope).where(
                BudgetScope.tenant_id == ctx.tenant_id,
                BudgetScope.dimension == dimension,
                BudgetScope.synthetic.is_(True),
            )
            if dimension == "tenant":
                query = query.where(BudgetScope.provider_connection_id.is_(None))
            else:
                provider = connection_for(self.db, ctx, authority.connection_id)
                query = query.join(
                    ProviderConnection, ProviderConnection.id == BudgetScope.provider_connection_id
                ).where(
                    ProviderConnection.provider == provider.provider,
                    ProviderConnection.provider_type == provider.provider_type,
                )
            existing = self.db.scalar(query)
            if existing is not None:
                return existing
        scope = BudgetScope(
            tenant_id=ctx.tenant_id,
            provider_connection_id=connection_id,
            dimension=dimension,
            synthetic=True,
            creator_id=ctx.actor_id,
        )
        self.db.add(scope)
        self.db.flush()
        return scope

    def set_limit(
        self,
        ctx: TenantContext,
        scope_id: UUID,
        currency: str,
        amount: Decimal,
        requests: int,
        *,
        synthetic: SyntheticAuthority | None = None,
        unit_caps: dict[str, str | int] | None = None,
    ) -> BudgetLimit:
        IdentityService(self.db).authorize(ctx, Permission.SETTINGS)
        try:
            Currency(currency)
        except ValueError:
            raise HTTPException(422, "INVALID_CURRENCY") from None
        validate_charge(amount)
        if requests < 0:
            raise HTTPException(422, "INVALID_CAP")
        scope = self.db.get(BudgetScope, scope_id)
        if scope is None or scope.tenant_id != ctx.tenant_id:
            raise HTTPException(403, "FOREIGN_SCOPE")
        if synthetic is not None and not self._authority(ctx, synthetic.connection_id, synthetic):
            raise HTTPException(403, "SYNTHETIC_ONLY")
        state = "SYNTHETIC_TEST_ONLY" if synthetic else "DRAFT"
        row = self.db.scalar(
            select(BudgetLimit).where(
                BudgetLimit.scope_id == scope.id, BudgetLimit.currency == currency, BudgetLimit.state == state
            )
        )
        if row is None:
            row = BudgetLimit(tenant_id=ctx.tenant_id, scope_id=scope.id, currency=currency, state=state)
            self.db.add(row)
        caps: dict[str, str | int] = {key: 0 for key in Units.model_fields}
        caps["requests"] = requests
        for key, value in (unit_caps or {}).items():
            if key not in caps or isinstance(value, bool):
                raise HTTPException(422, "INVALID_UNIT_CAP")
            parsed = Decimal(str(value))
            validate_charge(parsed)
            if key != "duration_seconds" and parsed != parsed.to_integral_value():
                raise HTTPException(422, "INVALID_UNIT_CAP")
            caps[key] = str(parsed)
        row.amount, row.requests, row.unit_caps = amount, requests, caps
        self.db.flush()
        IdentityService(self.db).audit(ctx, "budget.limit_saved", row.id, {"state": state})
        return row

    def reserve_budget(
        self,
        ctx: TenantContext,
        request: UsageReservationRequest,
        *,
        authority: SyntheticAuthority | None = None,
    ) -> BudgetReservation:
        IdentityService(self.db).authorize(ctx, Permission.SETTINGS)
        return self._reserve_checked(
            ctx,
            request,
            allowed=self._authority(ctx, request.provider_connection_id, authority),
            limit_state="SYNTHETIC_TEST_ONLY",
        )

    def _reserve_checked(
        self, ctx: UsageContext, request: UsageReservationRequest, *, allowed: bool, limit_state: str
    ) -> BudgetReservation:
        """Internal exact-scope accounting; caller has independently validated persisted authority."""
        try:
            Currency(request.currency)
        except ValueError:
            raise HTTPException(422, "INVALID_CURRENCY") from None
        prior = self.db.scalar(
            select(BudgetReservation)
            .where(
                BudgetReservation.tenant_id == ctx.tenant_id,
                BudgetReservation.provider_connection_id == request.provider_connection_id,
                BudgetReservation.operation_key == request.operation_key,
            )
            .execution_options(populate_existing=True)
        )
        payload = request.model_dump(mode="json")
        if prior:
            if prior.request != payload:
                raise HTTPException(409, "RESERVATION_REPLAY_CONFLICT")
        reason = (
            "EXTERNAL_AUTHORIZATION_UNAVAILABLE"
            if not allowed
            else "UNBOUNDED_PRICE"
            if request.amount is None
            else ""
        )
        scopes = list(
            self.db.scalars(
                select(BudgetScope)
                .where(BudgetScope.id.in_(request.scope_ids))
                .execution_options(populate_existing=True)
            )
        )
        provider = connection_for(self.db, ctx, request.provider_connection_id)

        def bound_to_connection(scope: BudgetScope) -> bool:
            if scope.dimension == "tenant":
                return scope.provider_connection_id is None
            if scope.dimension == "provider":
                anchor = self.db.get(ProviderConnection, scope.provider_connection_id, populate_existing=True)
                return (
                    anchor is not None
                    and anchor.tenant_id == ctx.tenant_id
                    and (anchor.provider, anchor.provider_type) == (provider.provider, provider.provider_type)
                )
            return scope.provider_connection_id == request.provider_connection_id

        if (
            len(set(request.scope_ids)) != len(request.scope_ids)
            or len(scopes) != len(request.scope_ids)
            or any(s.tenant_id != ctx.tenant_id or not bound_to_connection(s) for s in scopes)
        ):
            raise HTTPException(403, "FOREIGN_SCOPE")
        if {s.dimension for s in scopes} != {"tenant", "provider", "run", "call", "demo_session"}:
            reason = reason or "MISSING_BOUND"
        limits = list(
            self.db.scalars(
                select(BudgetLimit)
                .where(
                    BudgetLimit.scope_id.in_(request.scope_ids),
                    BudgetLimit.state == limit_state,
                )
                .order_by(BudgetLimit.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        if {limit.scope_id for limit in limits if limit.currency == request.currency} != {
            s.id for s in scopes
        }:
            reason = reason or "MISSING_BOUND"
        with localcontext() as precision:
            precision.prec = 80
            for limit in limits:
                rows = list(
                    self.db.scalars(
                        select(BudgetReservation)
                        .join(ReservationScope, ReservationScope.reservation_id == BudgetReservation.id)
                        .where(
                            ReservationScope.scope_id == limit.scope_id, BudgetReservation.status != "BLOCKED"
                        )
                        .execution_options(populate_existing=True)
                    )
                )
                # Money has no implicit FX. Quantities below remain scope-wide across
                # currencies and must satisfy every active currency row's unit caps.
                exposure = sum(
                    (r.retained_amount + r.spent_amount for r in rows if r.currency == limit.currency),
                    Decimal(0),
                )
                used_requests = sum(int(str(r.request["requests"])) for r in rows if r.status != "NO_EFFECT")
                if (
                    limit.currency == request.currency
                    and exposure + (Decimal(0) if prior else request.amount or Decimal(0)) > limit.amount
                ) or used_requests + (0 if prior else request.requests) > limit.requests:
                    reason = reason or "CAP_REACHED:" + next(
                        s.dimension for s in scopes if s.id == limit.scope_id
                    )
                actual_units = {}
                for r in rows:
                    final = self.db.scalar(
                        select(ReservationOutcome)
                        .where(ReservationOutcome.reservation_id == r.id)
                        .order_by(ReservationOutcome.created_at.desc())
                        .limit(1)
                    )
                    actual_units[r.id] = (
                        final.payload.get("units") if final and r.status == "VERIFIED" else None
                    ) or r.request["units"]
                for key, cap in limit.unit_caps.items():
                    incoming = Decimal(0) if prior else Decimal(str(request.units.model_dump()[key]))
                    used = sum(
                        (
                            Decimal(str(cast(dict[str, object], actual_units[r.id])[key]))
                            for r in rows
                            if r.status != "NO_EFFECT"
                        ),
                        Decimal(0),
                    )
                    if incoming + used > Decimal(cap):
                        reason = (
                            reason
                            or "UNIT_CAP_REACHED:"
                            + next(s.dimension for s in scopes if s.id == limit.scope_id)
                            + ":"
                            + key
                        )
        if prior:
            if reason and prior.status != "BLOCKED":
                raise HTTPException(403, reason)
            return prior
        reservation = BudgetReservation(
            tenant_id=ctx.tenant_id,
            provider_connection_id=request.provider_connection_id,
            operation_key=request.operation_key,
            currency=request.currency,
            original_amount=request.amount,
            retained_amount=request.amount if not reason else Decimal(0),
            request=payload,
            status="BLOCKED" if reason else "RESERVED",
            reason=reason or limit_state,
        )
        self.db.add(reservation)
        self.db.flush()
        for scope in scopes:
            self.db.add(
                ReservationScope(tenant_id=ctx.tenant_id, reservation_id=reservation.id, scope_id=scope.id)
            )
        self.db.flush()
        return reservation

    def reconcile_reservation(self, ctx: TenantContext, reservation_id: UUID, result: UsageOutcome) -> None:
        IdentityService(self.db).authorize(ctx, Permission.SETTINGS)
        row = self.db.scalar(
            select(BudgetReservation)
            .where(BudgetReservation.id == reservation_id, BudgetReservation.tenant_id == ctx.tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise HTTPException(403, "FOREIGN_RESERVATION")
        prior = self.db.scalar(
            select(ReservationOutcome).where(
                ReservationOutcome.reservation_id == row.id, ReservationOutcome.outcome_key == result.key
            )
        )
        payload = result.model_dump(mode="json")
        if prior:
            if prior.payload != payload:
                raise HTTPException(409, "RECONCILIATION_CONFLICT")
            return
        if row.status in {"BLOCKED", "NO_EFFECT", "VERIFIED"}:
            raise HTTPException(409, "RESERVATION_TERMINAL")
        if result.status == "NO_EFFECT":
            if not result.verified_unused or result.actual_amount not in {None, Decimal(0)}:
                raise HTTPException(422, "UNVERIFIED_RELEASE")
            row.released_amount, row.retained_amount = row.original_amount or Decimal(0), Decimal(0)
        elif result.status == "VERIFIED":
            if result.actual_amount is None or not result.verified_unused:
                raise HTTPException(422, "UNVERIFIED_REMAINDER")
            with localcontext() as precision:
                precision.prec = 80
                row.spent_amount = result.actual_amount
                row.released_amount = max(
                    Decimal(0), (row.original_amount or Decimal(0)) - result.actual_amount
                )
                row.retained_amount = Decimal(0)
        row.status = result.status
        self.db.add(ReservationOutcome(reservation_id=row.id, outcome_key=result.key, payload=payload))
        self.db.flush()
