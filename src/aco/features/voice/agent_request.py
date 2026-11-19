"""Run/action-bound voice preparation only; no human promotion or call-start authority."""

import json
from uuid import UUID

from fastapi import HTTPException
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.types import StrictModel
from aco.features.accounting.models import InvoiceSnapshot
from aco.features.agent.context import RunAuthority, current_run, selected_invoices
from aco.features.agent.models import AgentStep
from aco.features.identity.models import AuditEvent
from aco.features.tools.authorization import AgentActionContext, require_agent_action
from aco.features.tools.read_names import canonical_read_name
from aco.features.tools.schemas import InvoiceFact, ReadEvidence
from aco.features.voice.dto import VoiceSessionView
from aco.features.voice.runtime import VoiceRuntime
from aco.features.voice.service import VoiceService


class RequestVoiceCall(StrictModel):
    invoice_id: UUID
    invoice_ids: list[UUID] = Field(min_length=1, max_length=3)
    objective: str = Field(min_length=1, max_length=1000)


def validate_voice_request(db: Session, authority: RunAuthority, command: RequestVoiceCall) -> list[UUID]:
    run = current_run(db, authority)
    selected = set(selected_invoices(db, run))
    scope = set(command.invoice_ids)
    if (
        command.invoice_id not in scope
        or len(scope) != len(command.invoice_ids)
        or not scope.issubset(selected)
    ):
        raise HTTPException(403, "VOICE_COMMAND_OUTSIDE_RUN_SCOPE")
    reads = list(
        db.scalars(
            select(AgentStep).where(
                AgentStep.tenant_id == run.tenant_id,
                AgentStep.run_id == run.id,
                AgentStep.kind == "read",
                AgentStep.status == "completed",
                AgentStep.invoice_id.in_(scope),
            )
        )
    )
    customers: set[UUID] = set()
    accounts: set[UUID] = set()
    evidence_ids: list[UUID] = []
    for key in command.invoice_ids:
        invoice = db.get(InvoiceSnapshot, key, populate_existing=True)
        if invoice is None or invoice.tenant_id != run.tenant_id or not invoice.verified:
            raise HTTPException(403, "VOICE_COMMAND_INVOICE_DENIED")
        observed = [r for r in reads if r.invoice_id == key]
        if not {"get_invoice", "get_verified_payments"}.issubset(
            {canonical_read_name(r.tool_name) for r in observed}
        ):
            raise HTTPException(403, "VOICE_COMMAND_SOURCE_NOT_READ")
        step = next(r for r in observed if r.tool_name == "get_invoice")
        sources = [
            ReadEvidence.model_validate_json(json.dumps(r.output))
            for r in observed
            if canonical_read_name(r.tool_name) in {"get_invoice", "get_verified_payments"}
        ]
        if any(not source.available or source.invoice_id != key for source in sources):
            raise HTTPException(403, "VOICE_COMMAND_SOURCE_NOT_READ")
        facts = ReadEvidence.model_validate_json(json.dumps(step.output)).records
        if (
            not facts
            or not isinstance(facts[0], InvoiceFact)
            or facts[0].invoice_id != key
            or facts[0].customer_id != invoice.customer_id
            or facts[0].source.provider_connection_id != invoice.provider_connection_id
            or facts[0].source.source_revision != invoice.source_revision
        ):
            raise HTTPException(409, "SOURCE_CHANGED_REINVESTIGATE")
        customers.add(invoice.customer_id)
        accounts.add(invoice.provider_connection_id)
        evidence_ids.extend(
            r.id
            for r in observed
            if canonical_read_name(r.tool_name) in {"get_invoice", "get_verified_payments"}
        )
    if len(customers) != 1 or len(accounts) != 1:
        raise HTTPException(403, "VOICE_COMMAND_CUSTOMER_SCOPE_DENIED")
    return evidence_ids


def prepare_agent_voice_review(
    db: Session, ctx: AgentActionContext, command: RequestVoiceCall, runtime: VoiceRuntime | None
) -> VoiceSessionView:
    action = require_agent_action(db, ctx, "request_voice_call", command)
    evidence_ids = validate_voice_request(db, ctx.authority, command)
    if runtime is None:
        raise HTTPException(409, "VOICE_PREPARATION_RUNTIME_UNAVAILABLE")
    prepared = VoiceService(db, runtime).prepare_agent_call(ctx, command)
    db.add(
        AuditEvent(
            tenant_id=ctx.tenant_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.correlation_id,
            action="voice.prepared_by_agent",
            target_id=prepared.id,
            details={
                "run_id": str(ctx.authority.run_id),
                "action_id": str(action.id),
                "approval_id": str(prepared.approval_id),
                "evidence_step_ids": [str(v) for v in evidence_ids],
                "effect": "independent_review_only",
                "capture": "DISABLED",
                "call_started": False,
            },
        )
    )
    return prepared
