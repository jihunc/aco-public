"""One-way safety propagation from an evidenced incoming contact-stop request.

This grants no recipient review, verified-sender identity, financial change or resume.
"""

import json
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.actions.models import ScheduledAction
from aco.features.agent.context import RunAuthority, current_run, selected_invoices
from aco.features.agent.models import AgentStep
from aco.features.cases.dto import CommunicationEvidence, WireModel
from aco.features.cases.service import create_review_task, place_hold, validate_evidence
from aco.features.communications.models import CommunicationInvoiceLink
from aco.features.identity.models import AuditEvent
from aco.features.policies.models import CollectionHold
from aco.features.review.models import CollectionTask
from aco.features.tools.authorization import AgentActionContext, require_agent_action
from aco.features.tools.schemas import CommunicationFact, ReadEvidence
from aco.features.voice.evidence_dto import VoiceOperationalEvidence

REASON = "contact_stop_pending_review"


class RequestCollectionHold(WireModel):
    invoice_id: UUID
    customer_id: UUID
    evidence: CommunicationEvidence | VoiceOperationalEvidence


def validate_contact_stop(
    db: Session, authority: RunAuthority, command: RequestCollectionHold
) -> InvoiceSnapshot:
    if not isinstance(command.evidence, CommunicationEvidence):
        raise HTTPException(403, "CONTACT_STOP_SOURCE_KIND_DENIED")
    run = current_run(db, authority)
    invoice = db.get(InvoiceSnapshot, command.invoice_id, populate_existing=True)
    if (
        command.invoice_id not in selected_invoices(db, run)
        or invoice is None
        or invoice.tenant_id != run.tenant_id
        or invoice.customer_id != command.customer_id
        or not invoice.verified
    ):
        raise HTTPException(403, "CONTACT_STOP_SCOPE_DENIED")
    message = validate_evidence(db, run.tenant_id, invoice.customer_id, command.evidence, [invoice.id])
    if message.source_state not in {"stored", "synthetic_fixture"}:
        raise HTTPException(403, "CONTACT_STOP_SOURCE_UNAVAILABLE")
    links = list(
        db.scalars(
            select(CommunicationInvoiceLink).where(
                CommunicationInvoiceLink.tenant_id == run.tenant_id,
                CommunicationInvoiceLink.communication_id == message.id,
            )
        )
    )
    if not links or any(link.confidence not in {"1", "contextual_unverified_sender"} for link in links):
        raise HTTPException(403, "CONTACT_STOP_AMBIGUOUS_SOURCE")
    for link in links:
        linked = db.get(InvoiceSnapshot, link.invoice_id)
        if (
            linked is None
            or linked.tenant_id != run.tenant_id
            or linked.customer_id != invoice.customer_id
            or linked.provider_connection_id != invoice.provider_connection_id
            or not linked.verified
        ):
            raise HTTPException(403, "CONTACT_STOP_AMBIGUOUS_SOURCE")
    seen = False
    for step in db.scalars(
        select(AgentStep).where(
            AgentStep.run_id == run.id,
            AgentStep.tenant_id == run.tenant_id,
            AgentStep.invoice_id == invoice.id,
            AgentStep.tool_name == "get_communication_history",
            AgentStep.status == "completed",
        )
    ):
        for fact in ReadEvidence.model_validate_json(json.dumps(step.output)).records:
            if isinstance(fact, CommunicationFact) and fact.communication_id == message.id:
                offset = command.evidence.offset - fact.offset
                seen = seen or (
                    offset >= 0
                    and fact.excerpt[offset : offset + len(command.evidence.excerpt)]
                    == command.evidence.excerpt
                )
    if not seen:
        raise HTTPException(403, "CONTACT_STOP_SOURCE_NOT_READ")
    return invoice


def request_collection_hold(
    db: Session, ctx: "AgentActionContext | VoiceActionContext", command: RequestCollectionHold
) -> list[UUID]:
    from aco.features.voice.models import VoiceBusinessEvidence
    from aco.features.voice.tools import VoiceActionContext, require_voice_action

    if isinstance(ctx, VoiceActionContext) and isinstance(command.evidence, VoiceOperationalEvidence):
        action = require_voice_action(db, ctx, command, kind="request_collection_hold")
        invoice = db.get(InvoiceSnapshot, command.invoice_id, populate_existing=True)
        source = db.get(VoiceBusinessEvidence, command.evidence.voice_evidence_id)
        if source is None or source.fact_kind != "contact_stop" or invoice is None:
            raise HTTPException(403, "VOICE_CONTACT_STOP_SOURCE_DENIED")
        source_id = source.id
        source_key = "contact_stop_voice_evidence_id"
        marker = "voice-contact-stop:" + str(source_id)
    elif isinstance(ctx, AgentActionContext) and isinstance(command.evidence, CommunicationEvidence):
        action = require_agent_action(db, ctx, "request_collection_hold", command)
        invoice = validate_contact_stop(db, ctx.authority, command)
        source_id = command.evidence.communication_id
        source_key = "contact_stop_communication_id"
        marker = "contact-stop:" + str(source_id)
    else:
        raise HTTPException(403, "CONTACT_STOP_SOURCE_AUTHORITY_DENIED")
    # Resolve propagation on the server; the model cannot supply another invoice/customer set.
    affected = list(
        db.scalars(
            select(InvoiceSnapshot)
            .where(
                InvoiceSnapshot.tenant_id == ctx.tenant_id,
                InvoiceSnapshot.provider_connection_id == invoice.provider_connection_id,
                InvoiceSnapshot.customer_id == invoice.customer_id,
            )
            .order_by(InvoiceSnapshot.id)
            .with_for_update()
        )
    )
    changed = []
    for row in affected:
        existing = db.scalar(
            select(CollectionHold)
            .where(
                CollectionHold.tenant_id == ctx.tenant_id,
                CollectionHold.invoice_id == row.id,
                CollectionHold.reason == REASON,
                CollectionHold.evidence == marker,
            )
            .with_for_update()
        )
        if existing is not None:
            if existing.released_at is not None:
                raise HTTPException(409, "CONTACT_STOP_ALREADY_REVIEWED")
            changed.append(existing.id)
        else:
            changed.append(place_hold(db, ctx, row.id, REASON, marker))
    for pending in db.scalars(
        select(ScheduledAction)
        .where(
            ScheduledAction.tenant_id == ctx.tenant_id,
            ScheduledAction.provider_connection_id == invoice.provider_connection_id,
            ScheduledAction.invoice_id.in_([r.id for r in affected]),
            ScheduledAction.kind.in_(["policy_due", "approval_request"]),
            ScheduledAction.status.in_(["queued", "awaiting_approval"]),
        )
        .with_for_update()
    ):
        pending.status = "cancelled"
        pending.version += 1
        pending.automatic_resend = False
        pending.last_error = "CONTACT_STOP_PENDING_REVIEW"
        db.add(
            AuditEvent(
                tenant_id=ctx.tenant_id,
                actor_id=ctx.actor_id,
                correlation_id=ctx.correlation_id,
                action="action.cancelled_for_contact_stop",
                target_id=pending.id,
                details={"source_action_id": str(action.id)},
            )
        )
    task = db.scalar(
        select(CollectionTask).where(
            CollectionTask.tenant_id == ctx.tenant_id,
            CollectionTask.command[source_key].as_string() == str(source_id),
        )
    )
    if task is None:
        key = create_review_task(
            db,
            ctx,
            "Review customer contact-stop request",
            "Contact suppressed pending human review. Sender remains unverified. Source " + str(source_id),
            invoice.id,
        )
        task = db.get(CollectionTask, key)
        assert task is not None
        task.source_action_id, task.source_agent_run_id = action.id, action.agent_run_id
        task.source_voice_session_id = action.voice_session_id
        task.command = {
            source_key: str(source_id),
            "customer_id": str(invoice.customer_id),
            "hold_ids": list(map(str, changed)),
        }
    db.add(
        AuditEvent(
            tenant_id=ctx.tenant_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.correlation_id,
            action="collection.contact_stop_requested",
            target_id=action.id,
            details={
                "source_kind": "voice_operational"
                if source_key == "contact_stop_voice_evidence_id"
                else "communication",
                "source_id": str(source_id),
                "customer_id": str(invoice.customer_id),
                "sender_verified": False,
            },
        )
    )
    return changed


if TYPE_CHECKING:
    from aco.features.voice.tools import VoiceActionContext
