"""Independent model intent/outcome commits using the existing canonical usage ledger."""

from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from aco.core.types import CostQuality, ProviderCost, ProviderErrorCode
from aco.features.agent.context import RunAuthority, current_run
from aco.features.agent.models import AgentRun, AgentUsage
from aco.features.identity.models import ProviderConnection
from aco.features.usage.dto import ProviderUsageInput, Units
from aco.features.usage.models import BudgetReservation, UsageAttempt
from aco.features.usage.service import UsageService
from aco.integrations.ports.llm import LLMExecutionBlocked, LLMProviderAttempt, LLMRequestIntent


class LiveBudgetGate(Protocol):
    """Trusted composition only. T06 presently has no approved real-provider spend authority.

    An implementation must verify current global/tenant kills and approved live lane,
    price snapshots and all applicable T06 scopes, then reserve before returning.
    It must retain unknown/partial exposure on observation. It cannot be supplied by HTTP.
    """

    def reserve(self, db: Session, authority: RunAuthority, intent: LLMRequestIntent) -> UUID: ...
    def observe(self, db: Session, reservation_id: UUID, observation: LLMProviderAttempt) -> None: ...


class RunModelGuard:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        authority: RunAuthority,
        step_id: UUID,
        budget: LiveBudgetGate | None = None,
    ):
        self.sessions, self.authority, self.step_id, self.budget = sessions, authority, step_id, budget

    async def before_request(self, intent: LLMRequestIntent) -> None:
        async with self.sessions.begin() as db:

            def reserve(store: Session) -> None:
                run = current_run(store, self.authority)
                if (
                    run.authenticity != "LIVE"
                    or self.budget is None
                    or intent.tenant_id != run.tenant_id
                    or intent.connection_id != run.provider_connection_id
                    or intent.actor_id != run.worker_actor_id
                    or intent.correlation_id != run.correlation_id
                    or intent.model_route != run.model_route
                    or intent.model_id != run.model_id
                    or intent.prompt_version != run.prompt_version
                    or intent.schema_version != run.schema_version
                    or intent.input_token_bound > 32000
                    or intent.output_token_bound > 4096
                ):
                    raise LLMExecutionBlocked(ProviderErrorCode.AUTH_EXPIRED)
                prior = list(store.scalars(select(AgentUsage).where(AgentUsage.run_id == run.id)))
                if len(prior) >= 3 or any(
                    (attempt := store.get(UsageAttempt, p.attempt_id)) is None or attempt.outcome == "PENDING"
                    for p in prior
                ):
                    raise LLMExecutionBlocked(ProviderErrorCode.UNKNOWN_SIDE_EFFECT)
                reservation_id = self.budget.reserve(store, self.authority, intent)
                reservation = store.get(BudgetReservation, reservation_id, populate_existing=True)
                if (
                    reservation is None
                    or reservation.tenant_id != run.tenant_id
                    or reservation.provider_connection_id != run.provider_connection_id
                    or reservation.status != "RESERVED"
                    or reservation.operation_key != "agent:" + str(intent.attempt_id)
                ):
                    raise LLMExecutionBlocked(ProviderErrorCode.VALIDATION_FAILED)
                store.add(
                    UsageAttempt(
                        id=intent.attempt_id,
                        tenant_id=run.tenant_id,
                        provider_connection_id=run.provider_connection_id,
                        actor_id=run.worker_actor_id,
                        correlation_id=run.correlation_id,
                        operation="investigation",
                        outcome="PENDING",
                    )
                )
                store.flush()
                store.add(
                    AgentUsage(
                        tenant_id=run.tenant_id,
                        run_id=run.id,
                        step_id=self.step_id,
                        attempt_id=intent.attempt_id,
                        reservation_id=reservation_id,
                    )
                )
                run.provider_attempted = True

            await db.run_sync(reserve)

    async def after_request(self, intent: LLMRequestIntent, observation: LLMProviderAttempt) -> None:
        # Persist charged/uncertain outcomes even if the user cancelled or lost membership.
        async with self.sessions.begin() as db:

            def finish(store: Session) -> None:
                link = store.get(
                    AgentUsage,
                    (self.authority.tenant_id, self.authority.run_id, intent.attempt_id),
                    with_for_update=True,
                )
                attempt = store.get(UsageAttempt, intent.attempt_id, with_for_update=True)
                run = store.get(AgentRun, self.authority.run_id, with_for_update=True)
                if (
                    link is None
                    or attempt is None
                    or run is None
                    or observation.attempt_id != intent.attempt_id
                    or link.step_id != self.step_id
                ):
                    raise LLMExecutionBlocked(ProviderErrorCode.VALIDATION_FAILED)
                if attempt.outcome != "PENDING":
                    raise LLMExecutionBlocked(ProviderErrorCode.UNKNOWN_SIDE_EFFECT)
                connection = store.get(ProviderConnection, run.provider_connection_id)
                assert connection is not None
                usage = observation.usage
                unknown = (
                    observation.error is not None or usage.input_tokens is None or usage.output_tokens is None
                )
                outcome = "UNKNOWN_SIDE_EFFECT" if unknown else "VERIFIED"
                event = UsageService(store).record_verified(
                    self.authority,
                    ProviderUsageInput(
                        provider_connection_id=run.provider_connection_id,
                        operation_key="attempt:" + str(intent.attempt_id),
                        component="llm",
                        operation="investigation",
                        provider_kind="llm",
                        provider=connection.provider,
                        synthetic=False,
                        cost=ProviderCost(amount=None, currency="USD", quality=CostQuality.UNKNOWN),
                        units=Units(
                            requests=usage.requests,
                            input_tokens=usage.input_tokens or 0,
                            output_tokens=usage.output_tokens or 0,
                            cached_tokens=usage.cached_input_tokens or 0,
                        ),
                        native_operation_id=observation.provider_reference.external_id
                        if observation.provider_reference
                        else None,
                        model=observation.model_id,
                        outcome=outcome,
                    ),
                )
                attempt.event_id, attempt.outcome, attempt.error_reference = (
                    event.id,
                    outcome,
                    observation.request_id,
                )
                link.event_id = event.id
                link.observation = observation.model_dump(mode="json")
                run.requests += usage.requests
                run.input_tokens = (
                    None if usage.input_tokens is None else (run.input_tokens or 0) + usage.input_tokens
                )
                run.output_tokens = (
                    None if usage.output_tokens is None else (run.output_tokens or 0) + usage.output_tokens
                )
                if self.budget is not None and link.reservation_id is not None:
                    self.budget.observe(store, link.reservation_id, observation)

            await db.run_sync(finish)
