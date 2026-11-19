"""Retain a trusted accepted-submission read without changing submitted source bytes.

Internal producer only: a browser/model cannot install an alias. The caller must
first authorize/read through EmailPort and finalize its canonical UsageAttempt.
An absent RFC ID is incomplete metadata, never permission to resend or invent one.
"""

import re
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.features.communications.models import (
    Communication,
    CommunicationRfcIdentity,
    EmailDispatchBinding,
    EmailSubmission,
)
from aco.features.identity.models import ProviderConnection
from aco.features.usage.models import ProviderUsageEvent, UsageAttempt
from aco.integrations.ports.email_dto import EmailMessage, EmailResult

READ_OPERATION = "email_submission_get_message"


def preserve_observed_rfc_identity(
    db: Session,
    *,
    submission_id: UUID,
    result: EmailResult[EmailMessage],
    usage_event_id: UUID,
) -> CommunicationRfcIdentity | None:
    """Return the immutable observation, or None for unavailable RFC metadata.

    Caller owns transaction/authorization. This is evidence installation, not a
    new source of provider authority, and cannot itself perform a network read.
    """
    try:
        result = EmailResult[EmailMessage].model_validate_json(result.model_dump_json())
    except ValidationError:
        raise HTTPException(403, "RFC_IDENTITY_SOURCE_MISMATCH") from None
    submission = db.get(EmailSubmission, submission_id, with_for_update=True, populate_existing=True)
    binding = db.get(EmailDispatchBinding, submission.binding_id) if submission else None
    message = db.get(Communication, binding.communication_id) if binding else None
    connection = db.get(ProviderConnection, result.provider_connection_id)
    event = db.get(ProviderUsageEvent, usage_event_id, populate_existing=True)
    attempts = list(db.scalars(select(UsageAttempt).where(UsageAttempt.event_id == usage_event_id)))
    attempt = attempts[0] if len(attempts) == 1 else None
    observed = result.value
    if (
        submission is None
        or binding is None
        or message is None
        or connection is None
        or event is None
        or attempt is None
        or submission.state != "accepted"
        or not submission.provider_message_id
        or message.direction != "outgoing"
        or submission.tenant_id != result.tenant_id
        or binding.tenant_id != result.tenant_id
        or message.tenant_id != result.tenant_id
        or connection.tenant_id != result.tenant_id
        or observed.tenant_id != result.tenant_id
        or binding.email_connection_id != result.provider_connection_id
        or message.provider_connection_id != result.provider_connection_id
        or observed.provider_connection_id != result.provider_connection_id
        or observed.direction != "outgoing"
        or (submission.operation_id, binding.operation_id) != (message.operation_id, message.operation_id)
        or message.native_message_id not in {None, submission.provider_message_id}
        or observed.provider_ref.external_id != submission.provider_message_id
        or observed.provider_ref.connection_id != result.provider_connection_id
        or observed.provider_ref.provider != connection.provider
        or result.provider_ref.provider != connection.provider
        or (event.tenant_id, event.scope_kind, event.scope_key)
        != (result.tenant_id, "TENANT", str(result.tenant_id))
        or event.provider_connection_id != result.provider_connection_id
        or event.provider_kind != "email"
        or event.provider != connection.provider
        or event.operation != READ_OPERATION
        or attempt.operation != READ_OPERATION
        or event.native_subject != submission.provider_message_id
        or event.native_operation_id != result.provider_request_id
        or event.correlation_id != result.correlation_id
        or (attempt.tenant_id, attempt.provider_connection_id, attempt.correlation_id)
        != (result.tenant_id, result.provider_connection_id, result.correlation_id)
        or attempt.outcome != "SUCCEEDED"
        or attempt.business_outcome != "OBSERVED"
        or not (event.synthetic == result.synthetic == observed.synthetic == message.synthetic)
        or result.native_identity != observed.native_identity
        or result.native_identity != ("SIMULATED" if result.synthetic else "PROVIDER")
        or result.authenticity != observed.authenticity
        or result.authenticity != ("SIMULATED" if result.synthetic else "VERIFIED")
    ):
        raise HTTPException(403, "RFC_IDENTITY_SOURCE_MISMATCH")
    rfc_id = observed.message_id
    if rfc_id is None:
        return None
    # Retain exact casing; reject a list, folded header, controls, whitespace, or non-ID value.
    if (
        len(rfc_id) > 998
        or re.fullmatch(r"<[\x21-\x3b\x3d\x3f\x41-\x7e]+@[\x21-\x3b\x3d\x3f\x41-\x7e]+>", rfc_id) is None
    ):
        raise HTTPException(403, "RFC_IDENTITY_INVALID_MESSAGE_ID")
    material = dict(
        tenant_id=result.tenant_id,
        provider_connection_id=result.provider_connection_id,
        communication_id=message.id,
        submission_id=submission.id,
        usage_event_id=event.id,
        provider=connection.provider,
        native_message_id=submission.provider_message_id,
        rfc_message_id=rfc_id,
        observation_reference=result.provider_ref.external_id,
        observation_origin=result.reference_origin,
        provider_request_id=result.provider_request_id,
        observed_at=result.observed_at,
        native_identity=result.native_identity,
        synthetic=result.synthetic,
    )
    existing = db.scalar(
        select(CommunicationRfcIdentity).where(CommunicationRfcIdentity.usage_event_id == event.id)
    )
    if existing is not None:
        # Normalize legacy/driver naive UTC representations without altering aware offsets.
        from datetime import UTC

        stored_time = existing.observed_at
        if stored_time.tzinfo is None:
            stored_time = stored_time.replace(tzinfo=UTC)
        if stored_time != result.observed_at or any(
            getattr(existing, key) != value for key, value in material.items() if key != "observed_at"
        ):
            raise HTTPException(409, "RFC_IDENTITY_OBSERVATION_CONFLICT")
        return existing
    identity = CommunicationRfcIdentity(**material)
    db.add(identity)
    db.flush()
    return identity
