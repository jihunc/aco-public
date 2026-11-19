"""Exact selected-call reads and evidence validation; never generic actor permissions."""

from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.cases.dto import UpsertCase
from aco.features.cases.identity import issue_identity
from aco.features.cases.models import CaseInvoiceLink, CollectionCase
from aco.features.cases.service import validate_evidence
from aco.features.communications.models import Communication, CommunicationInvoiceLink
from aco.features.promises.dto import RecordPromise
from aco.features.promises.models import PaymentPromise, PromiseInvoiceLink
from aco.features.voice.models import VoiceInvocation, VoiceSession
from aco.features.voice.service import source_links


def read_records(
    db: Session, row: VoiceSession, invoice: InvoiceSnapshot, tool: str
) -> list[dict[str, object]]:
    if tool == "get_communication_history":
        messages = db.scalars(
            select(Communication)
            .join(CommunicationInvoiceLink, CommunicationInvoiceLink.communication_id == Communication.id)
            .where(
                Communication.tenant_id == row.tenant_id,
                CommunicationInvoiceLink.tenant_id == row.tenant_id,
                CommunicationInvoiceLink.invoice_id == invoice.id,
            )
            .order_by(Communication.event_at.desc(), Communication.id)
            .limit(51)
        ).unique()
        return [
            dict(
                communication_id=str(m.id),
                direction=m.direction,
                source_state=m.source_state,
                offset=0,
                excerpt=str(m.content.get("text_body", ""))[:2000],
                text_truncated=len(str(m.content.get("text_body", ""))) > 2000,
            )
            for m in messages
        ]
    if tool == "find_existing_cases":
        cases = db.scalars(
            select(CollectionCase)
            .join(CaseInvoiceLink, CaseInvoiceLink.case_id == CollectionCase.id)
            .where(
                CollectionCase.tenant_id == row.tenant_id,
                CaseInvoiceLink.tenant_id == row.tenant_id,
                CaseInvoiceLink.invoice_id == invoice.id,
            )
            .order_by(CollectionCase.id)
            .limit(51)
        ).unique()
        return [
            dict(
                case_id=str(c.id),
                version=c.version,
                customer_id=str(c.customer_id),
                issue_type=c.issue_type,
                claim_key=c.claim_key,
                status=c.status,
            )
            for c in cases
        ]
    if tool == "get_payment_promises":
        promises = db.scalars(
            select(PaymentPromise)
            .join(PromiseInvoiceLink, PromiseInvoiceLink.promise_id == PaymentPromise.id)
            .where(
                PaymentPromise.tenant_id == row.tenant_id,
                PromiseInvoiceLink.tenant_id == row.tenant_id,
                PromiseInvoiceLink.invoice_id == invoice.id,
            )
            .order_by(PaymentPromise.id)
            .limit(51)
        ).unique()
        return [
            dict(promise_id=str(p.id), version=p.version, customer_id=str(p.customer_id), status=p.status)
            for p in promises
        ]
    raise HTTPException(403, "VOICE_READ_TOOL_DENIED")


def validate_fact_scope(db: Session, row: VoiceSession, primary: UUID, raw: BaseModel) -> InvoiceSnapshot:
    from aco.features.voice.dto import VoiceCaseFactCommand, VoiceHoldFactCommand, VoicePromiseFactCommand

    if isinstance(raw, VoiceCaseFactCommand):
        affected = set(raw.case_facts.invoice_ids)
    elif isinstance(raw, VoicePromiseFactCommand):
        facts = raw.promise_facts
        if facts.scope == "ambiguous":
            if facts.invoice_ids or primary not in facts.candidate_invoice_ids:
                raise HTTPException(403, "VOICE_AMBIGUOUS_CANDIDATE_SCOPE_REQUIRED")
        elif not facts.invoice_ids:
            raise HTTPException(403, "VOICE_EXPLICIT_INVOICE_SCOPE_REQUIRED")
        affected = set(facts.invoice_ids) | set(facts.candidate_invoice_ids)
        affected.update(a.invoice_id for a in facts.allocations)
        affected.update(a.invoice_id for i in facts.installments for a in i.allocations)
    elif isinstance(raw, VoiceHoldFactCommand):
        affected = set(raw.hold_facts.invoice_ids)
    else:
        raise HTTPException(403, "VOICE_FACT_COMMAND_DENIED")
    selected = {link.invoice_id for link in source_links(db, row)}
    invoice = db.get(InvoiceSnapshot, primary, populate_existing=True)
    if invoice is None or primary not in affected or not affected.issubset(selected):
        raise HTTPException(403, "VOICE_DOMAIN_SCOPE_DENIED")
    reads = list(
        db.scalars(
            select(VoiceInvocation).where(
                VoiceInvocation.tenant_id == row.tenant_id,
                VoiceInvocation.session_id == row.id,
                VoiceInvocation.tool == "get_invoice",
                VoiceInvocation.result.is_not(None),
            )
        )
    )
    for key in affected:
        current = db.get(InvoiceSnapshot, key, populate_existing=True)
        if (
            current is None
            or current.tenant_id != row.tenant_id
            or not current.verified
            or current.customer_id != invoice.customer_id
            or current.provider_connection_id != invoice.provider_connection_id
        ):
            raise HTTPException(403, "VOICE_DOMAIN_CUSTOMER_DENIED")
        if not any(
            r.arguments.get("invoice_id") == str(key)
            and r.result
            and r.result.get("source_revision") == current.source_revision
            for r in reads
        ):
            raise HTTPException(403, "VOICE_READ_EVIDENCE_REQUIRED")
    return invoice


def validate_domain_scope(db: Session, row: VoiceSession, primary: UUID, raw: BaseModel) -> None:
    if not isinstance(raw, (UpsertCase, RecordPromise)):
        raise HTTPException(403, "VOICE_DOMAIN_COMMAND_DENIED")
    command = raw
    selected = {link.invoice_id for link in source_links(db, row)}
    affected = set(command.invoice_ids)
    if isinstance(command, RecordPromise):
        affected.update(command.candidate_invoice_ids)
        affected.update(a.invoice_id for a in command.allocations)
        affected.update(a.invoice_id for i in command.installments for a in i.allocations)
        # No implicit customer-wide scope from a voice command.
        if not command.invoice_ids:
            from aco.features.voice.evidence_dto import VoiceOperationalEvidence

            if (
                not isinstance(command.evidence, VoiceOperationalEvidence)
                or command.scope != "ambiguous"
                or primary not in command.candidate_invoice_ids
            ):
                raise HTTPException(403, "VOICE_EXPLICIT_INVOICE_SCOPE_REQUIRED")
    if primary not in affected or not affected.issubset(selected):
        raise HTTPException(403, "VOICE_DOMAIN_SCOPE_DENIED")
    reads = list(
        db.scalars(
            select(VoiceInvocation).where(
                VoiceInvocation.tenant_id == row.tenant_id,
                VoiceInvocation.session_id == row.id,
                VoiceInvocation.result.is_not(None),
            )
        )
    )
    for invoice_id in affected:
        invoice = db.get(InvoiceSnapshot, invoice_id, populate_existing=True)
        if (
            invoice is None
            or invoice.tenant_id != row.tenant_id
            or invoice.customer_id != command.customer_id
        ):
            raise HTTPException(403, "VOICE_DOMAIN_CUSTOMER_DENIED")
        if not any(
            r.tool == "get_invoice"
            and r.arguments.get("invoice_id") == str(invoice_id)
            and r.result
            and r.result.get("source_revision") == invoice.source_revision
            for r in reads
        ):
            raise HTTPException(403, "VOICE_READ_EVIDENCE_REQUIRED")
    message = validate_evidence(
        db,
        row.tenant_id,
        command.customer_id,
        command.evidence,
        command.invoice_ids or (command.candidate_invoice_ids if isinstance(command, RecordPromise) else []),
    )
    from aco.features.voice.evidence_dto import VoiceOperationalEvidence

    if isinstance(command.evidence, VoiceOperationalEvidence):
        # Immutable normalized facts have no communication/body span. Exact invocation
        # binding is checked by require_voice_action before this shared target guard.
        validate_target_scope(db, row, command, reads, selected)
        return
    if not isinstance(message, Communication) or message.source_state not in {"stored", "synthetic_fixture"}:
        raise HTTPException(403, "VOICE_SOURCE_UNAVAILABLE")
    primary_invoice = db.get(InvoiceSnapshot, primary, populate_existing=True)
    if primary_invoice is None:
        raise HTTPException(403, "VOICE_DOMAIN_SCOPE_DENIED")
    links = list(
        db.scalars(
            select(CommunicationInvoiceLink).where(
                CommunicationInvoiceLink.tenant_id == row.tenant_id,
                CommunicationInvoiceLink.communication_id == message.id,
            )
        )
    )
    for link in links:
        linked = db.get(InvoiceSnapshot, link.invoice_id, populate_existing=True)
        if (
            link.confidence not in {"1", "contextual_unverified_sender"}
            or (
                link.confidence == "contextual_unverified_sender"
                and link.provenance not in {"origin_thread", "explicit_reference", "contextual_inference"}
            )
            or linked is None
            or linked.tenant_id != row.tenant_id
            or not linked.verified
            or linked.customer_id != primary_invoice.customer_id
            or linked.provider_connection_id != primary_invoice.provider_connection_id
        ):
            raise HTTPException(403, "VOICE_SOURCE_AMBIGUOUS")
    seen = False
    for read in reads:
        if (
            read.tool != "get_communication_history"
            or read.arguments.get("invoice_id") != str(primary)
            or not read.result
        ):
            continue
        facts = read.result.get("records")
        if not isinstance(facts, list):
            continue
        for fact in facts:
            if isinstance(fact, dict) and fact.get("communication_id") == str(message.id):
                span = fact.get("excerpt")
                seen |= (
                    isinstance(span, str)
                    and span[
                        command.evidence.offset : command.evidence.offset + len(command.evidence.excerpt)
                    ]
                    == command.evidence.excerpt
                )
    if not seen:
        raise HTTPException(403, "VOICE_SOURCE_NOT_READ")
    validate_target_scope(db, row, command, reads, selected)


def validate_target_scope(
    db: Session,
    row: VoiceSession,
    command: UpsertCase | RecordPromise,
    reads: list[VoiceInvocation],
    selected: set[UUID],
) -> None:
    target = command.matched_case_id if isinstance(command, UpsertCase) else command.replaces_promise_id
    if isinstance(command, UpsertCase):
        implicit = db.scalar(
            select(CollectionCase)
            .where(
                CollectionCase.tenant_id == row.tenant_id,
                CollectionCase.issue_identity
                == issue_identity(command.customer_id, command.issue_type, command.claim_key),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if implicit:
            if target and target != implicit.id:
                raise HTTPException(409, "VOICE_CASE_IDENTITY_CONFLICT")
            target = implicit.id
    if target:
        tool, key = (
            ("find_existing_cases", "case_id")
            if isinstance(command, UpsertCase)
            else ("get_payment_promises", "promise_id")
        )
        observed = False
        for read in reads:
            facts = read.result.get("records") if read.result else None
            if read.tool == tool and isinstance(facts, list):
                observed |= any(isinstance(fact, dict) and fact.get(key) == str(target) for fact in facts)
        if not observed:
            raise HTTPException(403, "VOICE_TARGET_NOT_READ")
        target_model, target_field = (
            (CaseInvoiceLink, CaseInvoiceLink.case_id)
            if isinstance(command, UpsertCase)
            else (PromiseInvoiceLink, PromiseInvoiceLink.promise_id)
        )
        target_invoices = set(
            db.scalars(
                select(target_model.invoice_id).where(
                    target_model.tenant_id == row.tenant_id, target_field == target
                )
            )
        )
        if not target_invoices.issubset(selected):
            raise HTTPException(403, "VOICE_TARGET_SCOPE_DENIED")
