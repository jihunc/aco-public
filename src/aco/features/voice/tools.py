"""Voice-only exact invocation authority; it grants no generic worker or human permissions."""

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from aco.features.actions.models import ScheduledAction
from aco.features.voice.models import VoiceInvocation
from aco.integrations.ports.voice import CallContext

if TYPE_CHECKING:
    from aco.features.voice.runtime import VoiceRuntime


@dataclass(frozen=True)
class VoiceActionContext:
    call: CallContext
    invocation_id: UUID
    action_id: UUID
    runtime: "VoiceRuntime"

    @property
    def tenant_id(self) -> UUID:
        return self.call.tenant_id

    @property
    def actor_id(self) -> UUID:
        return self.call.actor_id

    @property
    def correlation_id(self) -> UUID:
        return self.call.correlation_id


def require_voice_action(
    db: Session, ctx: VoiceActionContext, command: BaseModel, *, kind: str = "create_collection_task"
) -> ScheduledAction:
    row = ctx.runtime.current(db, ctx.call)
    invocation = db.get(VoiceInvocation, ctx.invocation_id)
    action = db.get(ScheduledAction, ctx.action_id)
    tools = {
        "create_collection_task": ("create_collection_task", None),
        "upsert_collection_case": ("upsert_case", "case_command"),
        "record_payment_promise": ("record_promise", "promise_command"),
        "request_collection_hold": ("request_collection_hold", None),
    }
    expected = tools.get(kind)
    payload = (
        invocation.arguments.get(expected[1])
        if invocation and expected and expected[1]
        else invocation.arguments
        if invocation
        else None
    )
    from aco.features.voice.dto import VoiceCaseFactCommand, VoiceHoldFactCommand, VoicePromiseFactCommand
    from aco.features.voice.evidence_dto import VoiceOperationalEvidence

    fact_wrapper = isinstance(command, (VoiceCaseFactCommand, VoicePromiseFactCommand, VoiceHoldFactCommand))
    evidence = getattr(command, "evidence", None)
    if fact_wrapper and invocation:
        payload = invocation.arguments
    elif isinstance(evidence, VoiceOperationalEvidence) and invocation:
        from aco.features.cases.dto import UpsertCase
        from aco.features.promises.dto import RecordPromise
        from aco.features.tools.contact_stop import RequestCollectionHold
        from aco.features.voice.evidence import derived_command, validate_reference

        if not isinstance(command, (UpsertCase, RecordPromise, RequestCollectionHold)):
            raise HTTPException(403, "VOICE_ACTION_SOURCE_DENIED")
        source = validate_reference(
            db,
            row.tenant_id,
            command.customer_id,
            evidence,
            getattr(command, "invoice_ids", [action.invoice_id] if action else []),
        )
        if (
            source.session_id != row.id
            or source.invocation_id != invocation.id
            or action is None
            or source.action_id != action.id
        ):
            raise HTTPException(403, "VOICE_ACTION_SOURCE_DENIED")
        payload = derived_command(source).model_dump(mode="json")
    if (
        expected is None
        or row.state not in {"active", "connected"}
        or invocation is None
        or action is None
        or invocation.tenant_id != row.tenant_id
        or invocation.session_id != row.id
        or invocation.tool != expected[0]
        or payload != command.model_dump(mode="json")
        or invocation.action_id != action.id
        or invocation.result is not None
        or action.kind != "voice_internal"
        or action.tenant_id != row.tenant_id
        or action.voice_session_id != row.id
        or action.voice_invocation_id != invocation.id
        or action.status != "executing"
        or action.command != invocation.arguments
        or invocation.arguments.get("invoice_id") != str(action.invoice_id)
        or action.worker_actor_id != row.actor_id
        or action.initiator_id != row.initiator_id
        or action.lease_until != row.expires_at
        or action.lease_token is None
    ):
        raise HTTPException(403, "VOICE_ACTION_SCOPE_DENIED")
    if fact_wrapper:
        from aco.features.voice.domain import validate_fact_scope

        validate_fact_scope(db, row, action.invoice_id, command)
    elif kind == "request_collection_hold":
        if not isinstance(evidence, VoiceOperationalEvidence):
            raise HTTPException(403, "VOICE_CONTACT_STOP_SOURCE_REQUIRED")
    elif kind != "create_collection_task":
        from aco.features.voice.domain import validate_domain_scope

        validate_domain_scope(db, row, action.invoice_id, command)
    return action
