"""One scoped metadata read after acceptance; never a send retry or general worker read."""

from uuid import UUID, uuid5

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from aco.core.context import ServiceContext
from aco.core.types import CostQuality, ProviderCost, ProviderFailure, ProviderReference
from aco.features.communications.models import (
    CommunicationDeliveryEvent,
    EmailDispatchBinding,
    EmailExecutionActivation,
    EmailSubmission,
)
from aco.features.communications.provider_ingress import EmailContext
from aco.features.communications.provider_outbound import ProviderOutboundRuntime, persisted_outbound
from aco.features.communications.rfc_identity import READ_OPERATION, preserve_observed_rfc_identity
from aco.features.identity.models import now
from aco.features.identity.service import IdentityService, Permission
from aco.features.usage.dto import ProviderUsageInput, Units
from aco.features.usage.models import UsageAttempt, UsageSource
from aco.features.usage.service import UsageService
from aco.integrations.ports.email import EmailFailure
from aco.integrations.ports.email_dto import EmailMessage, EmailResult


async def observe_accepted_submission(
    db: AsyncSession, runtime: ProviderOutboundRuntime, ctx: ServiceContext, binding_id: UUID
) -> None:
    """Invoked only inside the original maintained action, before its lease completes.

    A durable attempt fences the single read, including crashes and unknown responses.
    The read never changes the accepted submission or its composed original. Expected
    metadata/authority failures leave an explicit unavailable event, not a failed send.
    """
    if not isinstance(ctx, ServiceContext):
        raise HTTPException(403, "SUBMISSION_READ_SCOPE")
    # Reject forged/stale workers before reading submission history or retaining any outcome.
    await db.run_sync(lambda store: IdentityService(store).authorize(ctx, Permission.INVOICE_READ))
    binding = await db.get(EmailDispatchBinding, binding_id)
    if binding is None or (binding.tenant_id, binding.action_id) != (ctx.tenant_id, ctx.action_id):
        raise HTTPException(403, "SUBMISSION_READ_SCOPE")
    activation = await db.get(EmailExecutionActivation, binding.activation_id)
    assert activation is not None
    # An absent/false selection performs no read. Enabled paths still require the full persisted validator.
    if (
        not activation.provider_binding
        or activation.provider_binding.get("observe_sent_identity") is not True
    ):
        return
    cap = await db.run_sync(lambda store: persisted_outbound(store, activation))
    if not cap.observe_sent_identity:
        return
    submission = await db.scalar(
        select(EmailSubmission).where(
            EmailSubmission.binding_id == binding.id, EmailSubmission.tenant_id == ctx.tenant_id
        )
    )
    if submission is None or submission.state != "accepted" or not submission.provider_message_id:
        raise HTTPException(403, "ACCEPTED_SUBMISSION_REQUIRED")
    submission_id = submission.id
    native = submission.provider_message_id
    try:
        direction, key = native.split(":")
        UUID(key)
        if direction != "sent":
            raise ValueError()
    except ValueError:
        raise HTTPException(403, "SUBMISSION_READ_SCOPE") from None
    reference = ProviderReference(provider=cap.provider, connection_id=cap.connection_id, external_id=native)
    attempt_id = uuid5(submission_id, READ_OPERATION)
    event_key = f"rfc-identity:{submission_id}"
    if await db.scalar(
        select(CommunicationDeliveryEvent.id).where(
            CommunicationDeliveryEvent.tenant_id == ctx.tenant_id,
            CommunicationDeliveryEvent.provider_connection_id == cap.connection_id,
            CommunicationDeliveryEvent.event_key == event_key,
        )
    ):
        return

    async def authorize(actual: EmailContext, operation: str, material: object | None, /) -> None:
        if (
            actual != ctx
            or not isinstance(actual, ServiceContext)
            or operation != "get_message"
            or material != reference
        ):
            raise HTTPException(403, "SUBMISSION_READ_SCOPE")

        def current(store: Session) -> None:
            IdentityService(store).authorize(ctx, Permission.INVOICE_READ)
            if runtime.validate(store, activation) != cap:
                raise HTTPException(403, "SUBMISSION_READ_SCOPE")
            saved = store.get(EmailSubmission, submission_id, populate_existing=True)
            bound = store.get(EmailDispatchBinding, binding.id, populate_existing=True)
            if (
                saved is None
                or bound is None
                or (
                    saved.tenant_id,
                    saved.binding_id,
                    saved.state,
                    saved.provider_message_id,
                    bound.tenant_id,
                    bound.action_id,
                    bound.email_connection_id,
                    bound.operation_id,
                )
                != (
                    ctx.tenant_id,
                    binding.id,
                    "accepted",
                    native,
                    ctx.tenant_id,
                    ctx.action_id,
                    cap.connection_id,
                    saved.operation_id,
                )
            ):
                raise HTTPException(403, "SUBMISSION_READ_SCOPE")

        await db.run_sync(current)

    class Observation:
        event_id: UUID | None = None

        async def started(self, actual: EmailContext, operation: str, material: object | None, /) -> object:
            await authorize(actual, operation, material)
            async with runtime.sessions.begin() as evidence:
                if await evidence.get(UsageAttempt, attempt_id) is not None:
                    raise HTTPException(409, "SUBMISSION_READ_ALREADY_ATTEMPTED")
                evidence.add(
                    UsageAttempt(
                        id=attempt_id,
                        tenant_id=ctx.tenant_id,
                        provider_connection_id=cap.connection_id,
                        actor_id=ctx.actor_id,
                        correlation_id=ctx.correlation_id,
                        operation=READ_OPERATION,
                        outcome="ATTEMPTED",
                        business_outcome="PENDING",
                    )
                )
                await evidence.flush()
                if cap.evidence_mode == "LIVE":
                    live = runtime.live_authorization
                    assert live is not None
                    await evidence.run_sync(
                        lambda store: live.reserve(
                            store, ctx, cap.connection_id, "get_sent_message", attempt_id=attempt_id
                        )
                    )
            await authorize(actual, operation, material)
            return self

        async def finished(
            self,
            token: object,
            failure: ProviderFailure | None,
            provider_request_id: str | None,
            status_code: int | None,
            response_bytes: int,
            /,
        ) -> None:
            assert token is self
            async with runtime.sessions.begin() as evidence:
                attempt = await evidence.get(UsageAttempt, attempt_id, with_for_update=True)
                assert attempt is not None and attempt.event_id is None
                outcome = "SUCCEEDED" if failure is None else "FAILED"
                event = await evidence.run_sync(
                    lambda store: UsageService(store).record_verified(
                        ctx,
                        ProviderUsageInput(
                            provider_connection_id=cap.connection_id,
                            operation_key=f"attempt:{attempt_id}",
                            component="email",
                            operation=READ_OPERATION,
                            provider_kind="email",
                            provider=cap.provider,
                            synthetic=cap.evidence_mode == "MOCKED_HTTP",
                            units=Units(requests=1),
                            cost=ProviderCost(amount=None, currency="USD", quality=CostQuality.UNKNOWN),
                            native_operation_id=provider_request_id,
                            native_subject=native,
                            outcome=outcome,
                        ),
                    )
                )
                attempt.event_id, attempt.outcome = event.id, outcome
                attempt.error_reference = failure.code.value if failure else None
                evidence.add(
                    UsageSource(
                        event_id=event.id,
                        source_kind="email_submission",
                        source_id=submission_id,
                        component="rfc_identity",
                    )
                )
                self.event_id = event.id

    observation = Observation()
    result: EmailResult[EmailMessage] | None = None
    reason: str | None = None
    try:
        # Validate before resolving credentials and again inside the adapter/intent boundary.
        await authorize(ctx, "get_message", reference)
        port = runtime.port_factory(cap, authorize, observation)
        result = await port.get_message(ctx, reference)
        await authorize(ctx, "get_message", reference)
    except EmailFailure as error:
        reason = error.failure.code.value
    except HTTPException as error:
        reason = str(error.detail)
    except (ValueError, LookupError, RuntimeError):
        # Private configuration errors never expose their contents in durable public evidence.
        reason = "SUBMISSION_READ_CONFIGURATION_UNAVAILABLE"

    async with runtime.sessions.begin() as evidence:

        def retain(store: Session) -> None:
            attempt = store.get(UsageAttempt, attempt_id, with_for_update=True)
            state = "unavailable"
            alias_id: UUID | None = None
            detail = reason
            if (
                result is not None
                and reason is None
                and observation.event_id is not None
                and attempt is not None
            ):
                try:
                    with store.begin_nested():
                        attempt.business_outcome = "OBSERVED"
                        store.flush()
                        alias = preserve_observed_rfc_identity(
                            store,
                            submission_id=submission_id,
                            result=result,
                            usage_event_id=observation.event_id,
                        )
                        state = "incomplete" if alias is None else "observed"
                        alias_id = alias.id if alias is not None else None
                except HTTPException as error:
                    detail = str(error.detail)
            if attempt is not None and state == "unavailable":
                attempt.business_outcome = "FAILED"
            store.add(
                CommunicationDeliveryEvent(
                    tenant_id=ctx.tenant_id,
                    communication_id=binding.communication_id,
                    provider_connection_id=cap.connection_id,
                    event_key=event_key,
                    state=f"rfc_identity_{state}",
                    occurred_at=now(),
                    evidence={
                        "submission_id": str(submission_id),
                        "attempt_id": str(attempt_id),
                        "usage_event_id": str(observation.event_id) if observation.event_id else None,
                        "alias_id": str(alias_id) if alias_id else None,
                        "reason": detail,
                        "accepted_submission_unchanged": True,
                    },
                )
            )

        await evidence.run_sync(retain)
