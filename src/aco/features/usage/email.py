"""Independent email submission/usage intent and canonical outcomes survive caller rollback."""

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from aco.features.communications.provider_outbound import WireObservation

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aco.core.context import ActorContext
from aco.core.types import CostQuality, ProviderCost, ProviderErrorCode, ProviderFailure
from aco.features.communications.models import EmailDispatchBinding, EmailExecutionActivation, EmailSubmission
from aco.features.identity.models import ProviderConnection, now
from aco.features.usage.dto import ProviderUsageInput, Units
from aco.features.usage.models import UsageAttempt, UsageSource
from aco.features.usage.service import UsageService
from aco.integrations.ports.email import EmailFailure, EmailPort
from aco.integrations.ports.email_dto import DeliveryAttempt, OutboundEmail


class ObservedEmail:
    def __init__(
        self,
        db: AsyncSession,
        sessions: async_sessionmaker[AsyncSession],
        port: EmailPort,
        *,
        observation: "WireObservation | None" = None,
    ):
        self.db, self.sessions, self.port = db, sessions, port
        self.observation = observation
        self.provider = "demo_email"
        self.currency = "AUD"
        self.synthetic = True

    async def send(self, ctx: ActorContext, command: OutboundEmail, submission_id: UUID) -> DeliveryAttempt:
        from fastapi import HTTPException

        key = uuid4()
        async with self.sessions.begin() as evidence:
            await evidence.execute(text("SET LOCAL lock_timeout = '2s'"))
            submission = await evidence.scalar(
                select(EmailSubmission)
                .where(EmailSubmission.id == submission_id, EmailSubmission.tenant_id == ctx.tenant_id)
                .with_for_update()
            )
            if (
                submission is None
                or submission.operation_id != command.operation_id
                or submission.state not in {"prepared", "no_effect"}
            ):
                raise HTTPException(409, "EMAIL_OPERATION_FENCED")
            binding = await evidence.get(EmailDispatchBinding, submission.binding_id)
            activation = (
                await evidence.get(EmailExecutionActivation, binding.activation_id) if binding else None
            )
            if binding is None or activation is None or binding.tenant_id != ctx.tenant_id:
                raise HTTPException(403, "EMAIL_EXECUTION_REVIEW_REQUIRED")
            connection = await evidence.get(ProviderConnection, command.provider_connection_id)
            if (
                connection is None
                or connection.tenant_id != ctx.tenant_id
                or connection.provider not in {"demo_email", "resend"}
            ):
                raise HTTPException(403, "EMAIL_CONNECTION_CHANGED")
            self.synthetic = self.observation.synthetic if self.observation is not None else True
            self.provider = connection.provider
            self.currency = "USD" if self.provider == "resend" else "AUD"
            if self.provider == "resend" and (
                activation.provider_binding is None or self.observation is None
            ):
                raise HTTPException(403, "PROVIDER_OUTBOUND_RUNTIME_REQUIRED")
            used = (
                await evidence.scalar(
                    select(func.count())
                    .select_from(UsageAttempt)
                    .where(
                        UsageAttempt.tenant_id == ctx.tenant_id,
                        UsageAttempt.provider_connection_id == command.provider_connection_id,
                        UsageAttempt.operation == "send_email",
                        UsageAttempt.created_at >= activation.created_at,
                    )
                )
                or 0
            )
            if used >= activation.request_cap:
                raise HTTPException(409, "SYNTHETIC_REQUEST_CAP")
            evidence.add(
                UsageAttempt(
                    id=key,
                    tenant_id=ctx.tenant_id,
                    provider_connection_id=command.provider_connection_id,
                    actor_id=ctx.actor_id,
                    correlation_id=ctx.correlation_id,
                    operation="send_email",
                    outcome="PENDING",
                )
            )
            await evidence.flush()
            submission.state, submission.intent_at, submission.usage_attempt_id = "pending", now(), key
        transaction = self.db.sync_session.get_nested_transaction() or self.db.sync_session.get_transaction()
        assert transaction
        self.db.info.setdefault("usage_attempt_ids", {}).setdefault(transaction, []).append(key)
        try:
            result = await self.port.send_email(ctx, command)
            if (
                (
                    result.tenant_id,
                    result.provider_connection_id,
                    result.provider_ref.provider,
                    result.correlation_id,
                    result.operation_id,
                    result.cost.currency,
                )
                != (
                    ctx.tenant_id,
                    command.provider_connection_id,
                    self.provider,
                    ctx.correlation_id,
                    command.operation_id,
                    self.currency,
                )
                or result.message_ref.provider != self.provider
                or result.message_ref.connection_id != command.provider_connection_id
                or result.synthetic != self.synthetic
                or result.native_identity != ("SIMULATED" if self.synthetic else "PROVIDER")
            ):
                raise EmailFailure(
                    ProviderFailure(
                        code=ProviderErrorCode.UNKNOWN_SIDE_EFFECT, retryable=False, may_have_side_effect=True
                    )
                )
        except Exception as error:
            failure = (
                error.failure
                if isinstance(error, EmailFailure)
                else ProviderFailure(
                    code=ProviderErrorCode.UNKNOWN_SIDE_EFFECT, retryable=False, may_have_side_effect=True
                )
            )
            if self.observation is not None and not self.observation.started_wire:
                failure = ProviderFailure(
                    code=ProviderErrorCode.AUTH_EXPIRED, retryable=False, may_have_side_effect=False
                )
            await self.finish(ctx, command, submission_id, key, None, failure)
            raise EmailFailure(failure) from error
        await self.finish(ctx, command, submission_id, key, result, None)
        return result

    async def finish(
        self,
        ctx: ActorContext,
        command: OutboundEmail,
        submission_id: UUID,
        key: UUID,
        result: DeliveryAttempt | None,
        failure: ProviderFailure | None,
    ) -> None:
        async with self.sessions.begin() as evidence:
            row = await evidence.get(EmailSubmission, submission_id, with_for_update=True)
            attempt = await evidence.get(UsageAttempt, key)
            assert row and attempt
            assert result is not None or failure is not None
            outcome = "VERIFIED" if result else failure.code.value if failure else "UNKNOWN_SIDE_EFFECT"
            native = (
                result.provider_request_id
                or (result.provider_ref.external_id if result.reference_origin == "PROVIDER" else None)
                if result
                else self.observation.provider_request_id
                if self.observation
                else None
            )
            if self.observation is not None and not self.observation.started_wire:
                assert failure is not None and not failure.may_have_side_effect
                attempt.outcome, attempt.error_reference = "NO_WIRE", failure.code.value
                row.state, row.failure = "no_effect", failure.model_dump(mode="json")
                return
            event = await evidence.run_sync(
                lambda store: UsageService(store).record_verified(
                    ctx,
                    ProviderUsageInput(
                        provider_connection_id=command.provider_connection_id,
                        operation_key="attempt:" + str(key),
                        component="email",
                        operation="send_email",
                        provider_kind="email",
                        provider=self.provider,
                        synthetic=self.synthetic,
                        cost=result.cost
                        if result
                        else ProviderCost(amount=None, currency=self.currency, quality=CostQuality.UNKNOWN),
                        units=Units(requests=1, messages=1 if result else 0),
                        native_operation_id=native,
                        outcome=outcome,
                    ),
                )
            )
            attempt.event_id, attempt.outcome = event.id, outcome
            attempt.error_reference = failure.request_id if failure else None
            row.state = (
                "accepted"
                if result
                else "unknown_outcome"
                if failure and failure.may_have_side_effect
                else "no_effect"
            )
            row.provider_message_id = result.message_ref.external_id if result else None
            row.provider_request_id = native if result else failure.request_id if failure else None
            row.failure = failure.model_dump(mode="json") if failure else None
            evidence.add(
                UsageSource(event_id=event.id, source_kind="email_attempt", source_id=key, component="email")
            )
