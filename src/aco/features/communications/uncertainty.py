"""Project independently retained submission uncertainty under the canonical action lock."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.features.actions.models import ScheduledAction
from aco.features.communications.models import Communication, EmailDispatchBinding, EmailSubmission
from aco.features.policies.models import ContactReservation


def project_email_uncertainty(db: Session, action: ScheduledAction) -> None:
    """No provider operation or authority grant; preserve the existing submission fence."""
    binding = db.scalar(
        select(EmailDispatchBinding).where(
            EmailDispatchBinding.tenant_id == action.tenant_id,
            EmailDispatchBinding.action_id == action.id,
            EmailDispatchBinding.operation_key == action.operation_key,
        )
    )
    if binding is None:
        return
    submission = db.scalar(
        select(EmailSubmission).where(
            EmailSubmission.tenant_id == action.tenant_id,
            EmailSubmission.binding_id == binding.id,
            EmailSubmission.state.in_(["pending", "accepted", "unknown_outcome"]),
        )
    )
    if submission is None:
        return
    message = db.get(Communication, binding.communication_id)
    contact = db.get(ContactReservation, binding.contact_reservation_id)
    assert message is not None and message.tenant_id == action.tenant_id
    assert contact is not None and contact.tenant_id == action.tenant_id
    message.delivery_state = "unknown_outcome"
    contact.status = "unknown_outcome"
    contact.submitted_at = submission.intent_at
    contact.evidence = {
        "submission_id": str(submission.id),
        "reconciliation_required": True,
        "independent_state": submission.state,
    }
