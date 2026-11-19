"""Server-only bounded live integration grant. Credentials never install a grant.

This is the explicitly authorized synthetic integration lane, not production spending.
The installer must be invoked by the operator; there is deliberately no HTTP route.
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import AwareDatetime, Field
from sqlalchemy import JSON, DateTime, ForeignKey, ForeignKeyConstraint, Numeric, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from aco.core.context import TenantContext
from aco.core.live import (
    APPROVED_LIVE_AUTHORIZATIONS,
    DELIVERY_LIVE_AUTHORIZATION,
    LEGACY_LIVE_AUTHORIZATION,
)
from aco.core.types import ProviderErrorCode, Role, StrictModel
from aco.features.agent.context import RunAuthority, current_run
from aco.features.agent.models import AgentRun
from aco.features.identity.models import AuditEvent, Base, Membership, ProviderConnection, Record, Tenant, now
from aco.features.identity.service import IdentityService, Permission
from aco.features.usage.budgets import BudgetService
from aco.features.usage.dto import Units, UsageOutcome, UsageReservationRequest
from aco.features.usage.live_scopes import live_provider_scope
from aco.features.usage.models import BudgetLimit, BudgetReservation, BudgetScope, RateSnapshot
from aco.integrations.ports.llm import (
    LLMExecutionBlocked,
    LLMProviderAttempt,
    LLMRequestIntent,
    LLMRequestPolicy,
)

AUTHORIZATION = LEGACY_LIVE_AUTHORIZATION


class BoundedLiveGrant(Record, Base):
    __tablename__ = "agent_bounded_live_grants"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id"],
            ["provider_connections.tenant_id", "provider_connections.id"],
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    initiator_id: Mapped[UUID] = mapped_column(ForeignKey("identity_users.id"))
    membership_version: Mapped[int]
    provider_connection_id: Mapped[UUID]
    model_id: Mapped[str]
    authorization_ref: Mapped[str]
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(default=True)
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("agent_runs.id"), unique=True)
    scope_ids: Mapped[list[str]] = mapped_column(JSON)
    input_rate_id: Mapped[UUID] = mapped_column(ForeignKey("usage_rates.id"))
    output_rate_id: Mapped[UUID] = mapped_column(ForeignKey("usage_rates.id"))
    max_requests: Mapped[int]
    max_input_tokens: Mapped[int]
    max_output_tokens: Mapped[int]
    amount: Mapped[Decimal] = mapped_column(Numeric(38, 12))


class LiveGrantConfig(StrictModel):
    model_id: str = Field(min_length=1, max_length=100)
    expires_at: AwareDatetime
    price_source_at: AwareDatetime
    input_usd_per_token: Decimal = Field(gt=0)
    output_usd_per_token: Decimal = Field(gt=0)
    max_requests: int = Field(ge=1, le=3)
    max_input_tokens: int = Field(ge=1, le=64000)
    max_output_tokens: int = Field(ge=1, le=4096)
    amount: Decimal = Field(gt=0, le=Decimal("1"))


def install_bounded_live_grant(
    db: Session, *, tenant_id: UUID, initiator_id: UUID, authorization_ref: str, config: LiveGrantConfig
) -> BoundedLiveGrant:
    """Trusted operator composition; do not expose this installer to application users."""
    tenant = db.get(Tenant, tenant_id, with_for_update=True)
    member = db.scalar(
        select(Membership).where(Membership.tenant_id == tenant_id, Membership.user_id == initiator_id)
    )
    if (
        authorization_ref not in APPROVED_LIVE_AUTHORIZATIONS
        or (authorization_ref != DELIVERY_LIVE_AUTHORIZATION and config.max_input_tokens > 32000)
        or tenant is None
        or not tenant.is_demo
        or member is None
        or not member.active
        or config.expires_at <= now()
        or config.expires_at > now() + timedelta(hours=1)
        or config.price_source_at > now()
        or config.price_source_at < now() - timedelta(days=30)
    ):
        raise HTTPException(403, "BOUNDED_LIVE_AUTHORIZATION_REQUIRED")
    IdentityService(db).authorize(
        TenantContext(
            tenant_id=tenant_id, actor_id=initiator_id, role=Role(member.role), correlation_id=uuid4()
        ),
        Permission.INVESTIGATE,
    )
    connection = db.scalar(
        select(ProviderConnection).where(
            ProviderConnection.tenant_id == tenant_id,
            ProviderConnection.provider == "openai",
            ProviderConnection.provider_type == "llm",
            ProviderConnection.status == "READY",
        )
    )
    if connection is None:
        connection = ProviderConnection(
            tenant_id=tenant_id,
            provider="openai",
            provider_type="llm",
            status="READY",
            capabilities=["structured_output", "tool_calling"],
        )
        db.add(connection)
        db.flush()
    rates = []
    for unit, amount in [
        ("input_tokens", config.input_usd_per_token),
        ("output_tokens", config.output_usd_per_token),
    ]:
        rate = RateSnapshot(
            tenant_id=tenant_id,
            provider="openai",
            currency="USD",
            unit=unit,
            amount=amount,
            price_source_at=config.price_source_at,
            synthetic=False,
        )
        db.add(rate)
        db.flush()
        rates.append(rate.id)
    scopes = []
    for dimension in ("tenant", "provider", "run", "call", "demo_session"):
        scope = live_provider_scope(db, tenant_id, connection) if dimension == "provider" else None
        if dimension == "tenant":
            scope = db.scalar(
                select(BudgetScope).where(
                    BudgetScope.tenant_id == tenant_id,
                    BudgetScope.dimension == "tenant",
                    BudgetScope.synthetic.is_(False),
                    BudgetScope.provider_connection_id.is_(None),
                )
            )
        if scope is None:
            scope = BudgetScope(
                tenant_id=tenant_id,
                provider_connection_id=None if dimension == "tenant" else connection.id,
                dimension=dimension,
                synthetic=False,
                creator_id=initiator_id,
            )
            db.add(scope)
            db.flush()
        limit = db.scalar(
            select(BudgetLimit).where(
                BudgetLimit.scope_id == scope.id,
                BudgetLimit.currency == "USD",
                BudgetLimit.state == "BOUNDED_LIVE_TEST",
            )
        )
        caps = {
            "requests": config.max_requests,
            "input_tokens": config.max_input_tokens,
            "output_tokens": config.max_output_tokens,
            "cached_tokens": config.max_input_tokens,
            "messages": 0,
            "duration_seconds": 0,
        }
        if limit is None:
            limit = BudgetLimit(
                tenant_id=tenant_id,
                scope_id=scope.id,
                currency="USD",
                amount=config.amount,
                requests=config.max_requests,
                unit_caps=caps,
                state="BOUNDED_LIVE_TEST",
            )
            db.add(limit)
        elif dimension != "tenant":
            # A new grant may tighten but can never reset/raise an existing exposure cap.
            limit.amount = min(limit.amount, config.amount)
            limit.requests = min(limit.requests, config.max_requests)
            limit.unit_caps = {
                k: min(Decimal(str(limit.unit_caps[k])), Decimal(str(v))).__str__() for k, v in caps.items()
            }
        scopes.append(str(scope.id))
    grant = BoundedLiveGrant(
        tenant_id=tenant_id,
        initiator_id=initiator_id,
        membership_version=member.version,
        provider_connection_id=connection.id,
        model_id=config.model_id,
        authorization_ref=authorization_ref,
        expires_at=config.expires_at,
        scope_ids=scopes,
        input_rate_id=rates[0],
        output_rate_id=rates[1],
        max_requests=config.max_requests,
        max_input_tokens=config.max_input_tokens,
        max_output_tokens=config.max_output_tokens,
        amount=config.amount,
    )
    db.add(grant)
    db.flush()
    db.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor_id=initiator_id,
            correlation_id=uuid4(),
            action="agent.bounded_live_grant_installed",
            target_id=grant.id,
            details={"authorization_ref": authorization_ref},
        )
    )
    return grant


class BoundedLiveBudget:
    def __init__(self, grant_id: UUID, kill_switch: Callable[[], bool]):
        self.grant_id, self.kill_switch = grant_id, kill_switch

    def grant(self, db: Session, run: AgentRun) -> BoundedLiveGrant:
        grant = db.get(BoundedLiveGrant, self.grant_id, with_for_update=True, populate_existing=True)
        if (
            self.kill_switch()
            or grant is None
            or not grant.active
            or grant.expires_at <= now()
            or grant.authorization_ref not in APPROVED_LIVE_AUTHORIZATIONS
            or grant.tenant_id != run.tenant_id
            or grant.run_id != run.id
            or grant.initiator_id != run.initiator_id
            or grant.membership_version != run.membership_version
            or grant.provider_connection_id != run.provider_connection_id
            or grant.model_id != run.model_id
        ):
            raise LLMExecutionBlocked(ProviderErrorCode.AUTH_EXPIRED)
        return grant

    def reserve(self, db: Session, authority: RunAuthority, intent: LLMRequestIntent) -> UUID:
        run = current_run(db, authority)
        grant = self.grant(db, run)
        rates = [
            db.get(RateSnapshot, key, populate_existing=True)
            for key in (grant.input_rate_id, grant.output_rate_id)
        ]
        if any(
            r is None
            or r.synthetic
            or r.provider != "openai"
            or r.tenant_id != run.tenant_id
            or r.currency != "USD"
            or r.price_source_at > now()
            or r.price_source_at < now() - timedelta(days=30)
            for r in rates
        ):
            raise LLMExecutionBlocked(ProviderErrorCode.VALIDATION_FAILED)
        assert rates[0] is not None and rates[1] is not None
        amount = rates[0].amount * intent.input_token_bound + rates[1].amount * intent.output_token_bound
        request = UsageReservationRequest(
            operation_key="agent:" + str(intent.attempt_id),
            provider_connection_id=run.provider_connection_id,
            scope_ids=tuple(UUID(k) for k in grant.scope_ids),
            currency="USD",
            amount=amount,
            requests=1,
            units=Units(
                requests=1, input_tokens=intent.input_token_bound, output_tokens=intent.output_token_bound
            ),
        )
        reservation = BudgetService(db)._reserve_checked(
            authority, request, allowed=True, limit_state="BOUNDED_LIVE_TEST"
        )
        if reservation.status != "RESERVED":
            raise LLMExecutionBlocked(ProviderErrorCode.RATE_LIMITED)
        return reservation.id

    def observe(self, db: Session, reservation_id: UUID, observation: LLMProviderAttempt) -> None:
        row = db.get(BudgetReservation, reservation_id, with_for_update=True)
        # Billing remains UNKNOWN: retain full conservative exposure; no inferred zero/release.
        from aco.features.usage.models import ReservationOutcome

        if row is None:
            raise LLMExecutionBlocked(ProviderErrorCode.VALIDATION_FAILED)
        row.status = "UNKNOWN_SIDE_EFFECT"
        db.add(
            ReservationOutcome(
                reservation_id=row.id,
                outcome_key="provider:" + str(observation.attempt_id),
                payload=UsageOutcome(
                    key="provider:" + str(observation.attempt_id),
                    status="UNKNOWN_SIDE_EFFECT",
                    units=Units(
                        requests=observation.usage.requests,
                        input_tokens=observation.usage.input_tokens or 0,
                        output_tokens=observation.usage.output_tokens or 0,
                    ),
                ).model_dump(mode="json"),
            )
        )

    def policy(self, db: Session, run: AgentRun) -> LLMRequestPolicy:
        grant = self.grant(db, run)
        return LLMRequestPolicy(
            max_requests=min(3, grant.max_requests),
            max_tool_calls=8,
            max_input_tokens=grant.max_input_tokens,
            max_output_tokens=grant.max_output_tokens,
            timeout_seconds=50,
        )
