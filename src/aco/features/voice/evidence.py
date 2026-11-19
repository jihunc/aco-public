"""Immutable normalized business facts from an exact signed invocation; never speech capture."""

import hashlib
import json
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.actions.models import ScheduledAction
from aco.features.voice.evidence_dto import VoiceEvidenceView, VoiceOperationalEvidence
from aco.features.voice.models import VoiceBusinessEvidence, VoiceEventRecord, VoiceInvocation, VoiceSession


def facts_digest(facts: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def source_view(db: Session, source_id: UUID | None) -> VoiceEvidenceView | None:
    if source_id is None:
        return None
    row = db.get(VoiceBusinessEvidence, source_id)
    if row is None:
        raise HTTPException(409, "VOICE_BUSINESS_SOURCE_UNAVAILABLE")
    return VoiceEvidenceView(
        id=row.id,
        session_id=row.session_id,
        invocation_id=row.invocation_id,
        provider_call_id=row.provider_call_id,
        native_invocation_id=row.native_invocation_id,
        fact_kind=cast("LiteralFactKind", row.fact_kind),
        facts_json=json.dumps(row.facts, sort_keys=True, separators=(",", ":")),
        facts_hash=row.facts_hash,
        observed_at=row.created_at,
        synthetic=row.synthetic,
    )


def validate_reference(
    db: Session,
    tenant_id: UUID,
    customer_id: UUID,
    evidence: VoiceOperationalEvidence,
    invoice_ids: list[UUID],
) -> VoiceBusinessEvidence:
    row = db.get(VoiceBusinessEvidence, evidence.voice_evidence_id, populate_existing=True)
    if row is None or row.tenant_id != tenant_id or row.customer_id != customer_id:
        raise HTTPException(403, "VOICE_BUSINESS_SOURCE_SCOPE_DENIED")
    if row.facts_hash != evidence.facts_hash or row.facts_hash != facts_digest(row.facts):
        raise HTTPException(409, "VOICE_BUSINESS_FACTS_CHANGED")
    session = db.get(VoiceSession, row.session_id)
    invocation = db.get(VoiceInvocation, row.invocation_id)
    event = db.get(VoiceEventRecord, row.source_event_id)
    action = db.get(ScheduledAction, row.action_id)
    expected = {
        "case": ("upsert_case", "case_facts"),
        "promise": ("record_promise", "promise_facts"),
        "contact_stop": ("request_collection_hold", "hold_facts"),
    }.get(row.fact_kind)
    if (
        session is None
        or invocation is None
        or event is None
        or session.tenant_id != tenant_id
        or invocation.session_id != session.id
        or invocation.tenant_id != tenant_id
        or invocation.action_id != row.action_id
        or invocation.native_id != row.native_invocation_id
        or session.provider_call_id != row.provider_call_id
        or session.connection_id != row.provider_connection_id
        or event.tenant_id != tenant_id
        or event.session_id != session.id
        or event.kind != "tool.requested"
        or event.event_key != "tool:" + invocation.native_id
        or action is None
        or action.tenant_id != tenant_id
        or action.kind != "voice_internal"
        or action.voice_session_id != session.id
        or action.voice_invocation_id != invocation.id
        or action.status not in {"executing", "succeeded"}
        or action.command != invocation.arguments
        or expected is None
        or invocation.tool != expected[0]
        or invocation.arguments.get(expected[1]) != row.facts
        or row.source_state != "VOICE_OPERATIONAL_METADATA"
    ):
        raise HTTPException(403, "VOICE_BUSINESS_PROVENANCE_DENIED")
    scope = row.facts.get("invoice_ids")
    candidates = row.facts.get("candidate_invoice_ids", [])
    if (
        not isinstance(scope, list)
        or not isinstance(candidates, list)
        or not {str(i) for i in invoice_ids}.issubset(scope + candidates)
    ):
        raise HTTPException(403, "VOICE_BUSINESS_INVOICE_SCOPE_DENIED")
    for invoice_id in invoice_ids:
        invoice = db.get(InvoiceSnapshot, invoice_id, populate_existing=True)
        if (
            invoice is None
            or invoice.tenant_id != tenant_id
            or not invoice.verified
            or invoice.customer_id != customer_id
            or invoice.provider_connection_id != row.accounting_connection_id
        ):
            raise HTTPException(403, "VOICE_BUSINESS_CURRENT_SCOPE_DENIED")
    return row


def derived_command(row: VoiceBusinessEvidence) -> BaseModel:
    from aco.features.cases.dto import UpsertCase
    from aco.features.promises.dto import RecordPromise
    from aco.features.tools.contact_stop import RequestCollectionHold
    from aco.features.voice.dto import VoiceCaseFacts, VoiceHoldFacts, VoicePromiseFacts

    reference = VoiceOperationalEvidence(voice_evidence_id=row.id, facts_hash=row.facts_hash)
    if row.fact_kind == "case":
        facts = VoiceCaseFacts.model_validate_json(json.dumps(row.facts))
        return UpsertCase(
            customer_id=row.customer_id,
            issue_type=facts.issue_type,
            claim_key="voice:" + facts.claim_reference,
            substance="Unverified issue reported as normalized call business facts; not a transcript.",
            invoice_ids=facts.invoice_ids,
            evidence=reference,
            matching=facts.matching,
            matched_case_id=facts.matched_case_id,
            expected_version=facts.expected_version,
        )
    if row.fact_kind == "promise":
        facts_promise = VoicePromiseFacts.model_validate_json(json.dumps(row.facts))
        return RecordPromise(customer_id=row.customer_id, evidence=reference, **facts_promise.model_dump())
    if row.fact_kind == "contact_stop":
        facts_hold = VoiceHoldFacts.model_validate_json(json.dumps(row.facts))
        return RequestCollectionHold(
            invoice_id=facts_hold.invoice_ids[0], customer_id=row.customer_id, evidence=reference
        )
    raise HTTPException(403, "VOICE_FACT_KIND_DENIED")


def persist_fact_source(db: Session, ctx: "VoiceActionContext", command: BaseModel) -> BaseModel:
    from aco.features.voice.domain import validate_fact_scope
    from aco.features.voice.dto import VoiceCaseFactCommand, VoiceHoldFactCommand, VoicePromiseFactCommand
    from aco.features.voice.tools import require_voice_action

    kinds = {
        VoiceCaseFactCommand: ("case", "case_facts", "upsert_collection_case"),
        VoicePromiseFactCommand: ("promise", "promise_facts", "record_payment_promise"),
        VoiceHoldFactCommand: ("contact_stop", "hold_facts", "request_collection_hold"),
    }
    kind, field, action_kind = kinds[type(command)]
    action = require_voice_action(db, ctx, command, kind=action_kind)
    session = ctx.runtime.current(db, ctx.call)
    invoice = validate_fact_scope(db, session, action.invoice_id, command)
    invocation = db.get(VoiceInvocation, ctx.invocation_id)
    assert invocation
    source_event = db.scalar(
        select(VoiceEventRecord).where(
            VoiceEventRecord.tenant_id == ctx.tenant_id,
            VoiceEventRecord.session_id == session.id,
            VoiceEventRecord.event_key == "tool:" + invocation.native_id,
            VoiceEventRecord.kind == "tool.requested",
        )
    )
    if source_event is None:
        raise HTTPException(403, "SIGNED_VOICE_EVENT_REQUIRED")
    facts = cast(dict[str, object], command.model_dump(mode="json")[field])
    row = VoiceBusinessEvidence(
        tenant_id=ctx.tenant_id,
        session_id=session.id,
        invocation_id=invocation.id,
        source_event_id=source_event.id,
        action_id=action.id,
        provider_connection_id=session.connection_id,
        accounting_connection_id=invoice.provider_connection_id,
        customer_id=invoice.customer_id,
        provider_call_id=session.provider_call_id,
        native_invocation_id=invocation.native_id,
        fact_kind=kind,
        facts=facts,
        facts_hash=facts_digest(facts),
        synthetic=session.live_grant_id is None,
        evidence_mode="LIVE" if session.live_grant_id is not None else "MOCKED_HTTP",
        source_state="VOICE_OPERATIONAL_METADATA",
    )
    db.add(row)
    db.flush()
    return derived_command(row)


if TYPE_CHECKING:
    from aco.features.voice.tools import VoiceActionContext

LiteralFactKind = Literal["case", "promise", "contact_stop"]
