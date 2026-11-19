"""Resolve independent stored submission evidence without authorizing another send."""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.features.actions.models import ScheduledAction
from aco.features.communications.dto import DeliveryReconciliation
from aco.features.communications.models import (
    Communication,
    CommunicationDeliveryEvent,
    EmailDispatchBinding,
    EmailSubmission,
)
from aco.features.identity.models import now
from aco.features.identity.service import IdentityService, Permission
from aco.features.policies.models import ContactReservation


def reconcile_delivery(db: Session, ctx: TenantContext, action_id: UUID) -> DeliveryReconciliation:
    IdentityService(db).authorize(ctx, Permission.INVESTIGATE)
    action = db.scalar(
        select(ScheduledAction)
        .where(ScheduledAction.tenant_id == ctx.tenant_id, ScheduledAction.id == action_id)
        .with_for_update()
    )
    if action is None:
        raise HTTPException(404, "ACTION_NOT_FOUND")
    binding = db.scalar(
        select(EmailDispatchBinding).where(
            EmailDispatchBinding.tenant_id == ctx.tenant_id, EmailDispatchBinding.action_id == action.id
        )
    )
    if binding is None:
        raise HTTPException(409, "EMAIL_BINDING_REQUIRED")
    submission = db.scalar(
        select(EmailSubmission)
        .where(EmailSubmission.tenant_id == ctx.tenant_id, EmailSubmission.binding_id == binding.id)
        .with_for_update()
    )
    message = db.get(Communication, binding.communication_id)
    contact = db.get(ContactReservation, binding.contact_reservation_id)
    assert submission and message and contact
    # Accepted evidence was committed by the observed provider producer independently
    # of the rolled-back business result. It proves acceptance, never delivery/reply.
    if submission.state == "accepted" and submission.provider_message_id:
        message.native_message_id = submission.provider_message_id
        message.delivery_state = "accepted"
        contact.status = "accepted"
        contact.submitted_at = submission.intent_at
        contact.evidence = {"submission_id": str(submission.id), "state": "provider_acceptance_not_delivery"}
        # Provider acceptance can have a genuine message ID without a request-ID header.
        # The fallback is an application identity, never a fabricated vendor request.
        event_key = submission.provider_request_id or f"submission:{submission.id}:accepted"
        existing = db.scalar(
            select(CommunicationDeliveryEvent).where(
                CommunicationDeliveryEvent.tenant_id == ctx.tenant_id,
                CommunicationDeliveryEvent.provider_connection_id == binding.email_connection_id,
                CommunicationDeliveryEvent.event_key == event_key,
            )
        )
        if existing is None:
            db.add(
                CommunicationDeliveryEvent(
                    tenant_id=ctx.tenant_id,
                    communication_id=message.id,
                    provider_connection_id=binding.email_connection_id,
                    event_key=event_key,
                    state="accepted",
                    occurred_at=submission.intent_at or now(),
                    evidence={
                        "reconciled_submission_id": str(submission.id),
                        "provider_acceptance_not_delivery": True,
                        "reference_origin": "PROVIDER" if submission.provider_request_id else "APPLICATION",
                    },
                )
            )
    elif submission.state in {"pending", "unknown_outcome"}:
        message.delivery_state = "unknown_outcome"
        contact.status = "unknown_outcome"
    IdentityService(db).audit(ctx, "email.delivery_reconciled", message.id)
    return DeliveryReconciliation(
        action_id=action.id, state=message.delivery_state, communication_id=message.id
    )
