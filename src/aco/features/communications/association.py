"""Current-human or exact graph-action association; original and finance stay unchanged."""

import hashlib
import json
import re
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.features.accounting.models import Customer, InvoiceSnapshot
from aco.features.artifacts.models import Artifact
from aco.features.communications.association_dto import (
    CommunicationLinkCandidate,
    CommunicationLinkReview,
    LinkCommunication,
)
from aco.features.communications.models import (
    Communication,
    CommunicationInvoiceLink,
    CommunicationRfcIdentity,
    CommunicationSource,
    CommunicationThread,
)
from aco.features.identity.models import AuditEvent, ProviderConnection, now
from aco.features.identity.service import IdentityService, Permission
from aco.features.tools.authorization import AgentActionContext, require_agent_action


def message(db: Session, tenant_id: UUID, communication_id: UUID) -> Communication:
    row = db.scalar(
        select(Communication)
        .where(
            Communication.tenant_id == tenant_id,
            Communication.id == communication_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise HTTPException(404, "COMMUNICATION_NOT_FOUND")
    return row


def retained_original(db: Session, row: Communication) -> str:
    connection = db.get(ProviderConnection, row.provider_connection_id, populate_existing=True)
    if (
        row.channel != "email"
        or row.direction != "incoming"
        or row.kind == "OUTBOUND_ECHO"
        or row.source_state != "stored"
        or not row.message_sha256
        or connection is None
        or connection.tenant_id != row.tenant_id
        or connection.provider_type != "email"
    ):
        raise HTTPException(409, "RETAINED_INCOMING_EMAIL_REQUIRED")
    records = db.execute(
        select(CommunicationSource, Artifact)
        .join(
            Artifact,
            Artifact.id == CommunicationSource.artifact_id,
        )
        .where(
            CommunicationSource.tenant_id == row.tenant_id,
            CommunicationSource.communication_id == row.id,
            CommunicationSource.representation == "provider_original",
            Artifact.tenant_id == row.tenant_id,
        )
        .execution_options(populate_existing=True)
    ).all()
    if len(records) != 1:
        raise HTTPException(409, "ORIGINAL_SOURCE_UNAVAILABLE")
    source, artifact = records[0]
    if (
        artifact.sha256 != source.sha256
        or artifact.storage_state != "stored"
        or artifact.revoked_at is not None
        or artifact.consent_revoked
        or artifact.legal_hold_state != "none"
        or artifact.expires_at <= now()
    ):
        raise HTTPException(409, "ORIGINAL_SOURCE_UNAVAILABLE")
    return source.sha256


def reviewed_command(db: Session, link: CommunicationInvoiceLink) -> LinkCommunication:
    digest = link.claim_or_context_key.removeprefix("reviewed:")
    event = db.scalar(
        select(AuditEvent).where(
            AuditEvent.tenant_id == link.tenant_id,
            AuditEvent.target_id == link.communication_id,
            AuditEvent.action == "communication.invoices_linked",
            AuditEvent.details["command_sha256"].as_string() == digest,
        )
    )
    payload = event.details.get("command") if event else None
    if not isinstance(payload, str) or hashlib.sha256(payload.encode()).hexdigest() != digest:
        raise HTTPException(409, "COMMUNICATION_REVIEW_EVIDENCE_UNAVAILABLE")
    try:
        reviewed = LinkCommunication.model_validate_json(payload)
    except ValidationError:
        raise HTTPException(409, "COMMUNICATION_REVIEW_EVIDENCE_UNAVAILABLE") from None
    if (
        reviewed.evidence.communication_id != link.communication_id
        or link.invoice_id not in reviewed.invoice_ids
        or link.evidence_excerpt != reviewed.evidence.excerpt
        or link.evidence_offset != reviewed.evidence.offset
    ):
        raise HTTPException(409, "COMMUNICATION_REVIEW_EVIDENCE_UNAVAILABLE")
    return reviewed


def current_links(
    db: Session, row: Communication, by_id: dict[UUID, InvoiceSnapshot]
) -> list[CommunicationInvoiceLink]:
    """Validate retained reviewed scope before reusing current or RFC-parent links."""
    links = list(
        db.scalars(
            select(CommunicationInvoiceLink).where(
                CommunicationInvoiceLink.tenant_id == row.tenant_id,
                CommunicationInvoiceLink.communication_id == row.id,
            )
        )
    )
    for link in links:
        if link.classification != "reviewed_reference":
            continue
        reviewed = reviewed_command(db, link)
        reviewed_invoice = by_id.get(link.invoice_id)
        if reviewed_invoice is None or reviewed_invoice.customer_id != reviewed.customer_id:
            raise HTTPException(409, "COMMUNICATION_CUSTOMER_CHANGED")
    return links


def candidates(db: Session, row: Communication) -> list[CommunicationLinkCandidate]:
    """Only unique explicit references and exact retained origin/RFC context.

    No sender match is promoted to verified identity. All source-visible references,
    retained links and context must agree on one current accounting customer.
    """
    thread = db.get(CommunicationThread, row.thread_id, populate_existing=True)
    if (
        thread is None
        or thread.tenant_id != row.tenant_id
        or thread.provider_connection_id != row.provider_connection_id
    ):
        raise HTTPException(403, "COMMUNICATION_THREAD_SCOPE_MISMATCH")
    invoices = list(
        db.scalars(
            select(InvoiceSnapshot)
            .where(
                InvoiceSnapshot.tenant_id == row.tenant_id,
            )
            .execution_options(populate_existing=True)
        )
    )
    by_id = {invoice.id: invoice for invoice in invoices}
    modes: dict[UUID, str] = {}
    if thread.origin_invoice_id is not None:
        modes[thread.origin_invoice_id] = "origin_thread"
    stored_refs = row.content.get("references")
    refs = (
        {value for value in stored_refs if isinstance(value, str)} if isinstance(stored_refs, list) else set()
    )
    reply = row.content.get("in_reply_to")
    if isinstance(reply, str):
        refs.add(reply)
    if refs:
        aliases = select(CommunicationRfcIdentity.communication_id).where(
            CommunicationRfcIdentity.tenant_id == row.tenant_id,
            CommunicationRfcIdentity.provider_connection_id == row.provider_connection_id,
            CommunicationRfcIdentity.rfc_message_id.in_(refs),
        )
        parents = list(
            db.scalars(
                select(Communication).where(
                    Communication.tenant_id == row.tenant_id,
                    Communication.provider_connection_id == row.provider_connection_id,
                    Communication.id != row.id,
                    or_(
                        Communication.content["message_id"].as_string().in_(refs),
                        Communication.id.in_(aliases),
                    ),
                )
            )
        )
        if len({p.thread_id for p in parents} | {row.thread_id}) > 1:
            raise HTTPException(409, "COMMUNICATION_CONTEXT_AMBIGUOUS")
        for parent in parents:
            for link in current_links(db, parent, by_id):
                modes.setdefault(link.invoice_id, "contextual_inference")
    body = row.content.get("text_body")
    if not isinstance(body, str) or not body:
        raise HTTPException(409, "EXACT_TEXT_SOURCE_REQUIRED")
    for invoice in invoices:
        if re.search(r"(?<![\w-])" + re.escape(invoice.number) + r"(?![\w-])", body):
            if sum(other.number == invoice.number for other in invoices) != 1:
                raise HTTPException(409, "COMMUNICATION_REFERENCE_AMBIGUOUS")
            if modes.get(invoice.id) != "origin_thread":
                modes[invoice.id] = "explicit_reference"
    linked = {link.invoice_id for link in current_links(db, row, by_id)}
    scoped = [by_id.get(key) for key in set(modes) | linked]
    if any(invoice is None or not invoice.verified for invoice in scoped):
        raise HTTPException(409, "COMMUNICATION_REFERENCE_STALE")
    scopes = {(invoice.customer_id, invoice.provider_connection_id) for invoice in scoped if invoice}
    if len(scopes) != 1:
        raise HTTPException(409, "COMMUNICATION_CUSTOMER_AMBIGUOUS")
    customer_id, connection_id = next(iter(scopes))
    if thread.customer_id is not None and thread.customer_id != customer_id:
        raise HTTPException(409, "COMMUNICATION_CUSTOMER_CHANGED")
    customer = db.get(Customer, customer_id, populate_existing=True)
    connection = db.get(ProviderConnection, connection_id, populate_existing=True)
    if (
        customer is None
        or customer.tenant_id != row.tenant_id
        or customer.provider_connection_id != connection_id
        or connection is None
        or connection.tenant_id != row.tenant_id
        or connection.provider_type != "accounting"
        or connection.status != "READY"
    ):
        raise HTTPException(409, "ACCOUNTING_CONNECTION_UNAVAILABLE")
    return [
        CommunicationLinkCandidate.model_validate(
            {
                "invoice_id": key,
                "customer_id": customer_id,
                "accounting_connection_id": connection_id,
                "number": by_id[key].number,
                "provenance": mode,
            }
        )
        for key, mode in sorted(modes.items(), key=lambda item: str(item[0]))
    ]


def link_review(db: Session, ctx: TenantContext, communication_id: UUID) -> CommunicationLinkReview:
    if not isinstance(ctx, TenantContext):
        raise HTTPException(403, "HUMAN_CONTEXT_REQUIRED")
    IdentityService(db).authorize(ctx, Permission.INVOICE_READ)
    row = message(db, ctx.tenant_id, communication_id)
    choices: list[CommunicationLinkCandidate] = []
    reasons: list[str] = []
    source_hash: str | None = None
    try:
        source_hash = retained_original(db, row)
        choices = candidates(db, row)
        IdentityService(db).authorize(ctx, Permission.TASK_WRITE)
    except HTTPException as error:
        reasons.append(str(error.detail))
    return CommunicationLinkReview(
        communication_id=row.id,
        communication_version=row.version,
        source_sha256=source_hash,
        can_link=bool(choices) and not reasons,
        reasons=reasons,
        candidates=choices,
    )


def link_communication(
    db: Session, ctx: TenantContext | AgentActionContext, command: LinkCommunication
) -> UUID:
    if isinstance(ctx, TenantContext):
        IdentityService(db).authorize(ctx, Permission.TASK_WRITE)
    elif isinstance(ctx, AgentActionContext):
        action = require_agent_action(db, ctx, "link_communication", command)
        if action.invoice_id not in command.invoice_ids:
            raise HTTPException(403, "COMMAND_INVOICE_CONFLICT")
        from aco.features.tools.gateway import validate_domain_scope

        validate_domain_scope(db, ctx.authority, command, action.invoice_id)
    else:
        raise HTTPException(403, "INVALID_ACTOR_CONTEXT")
    row = message(db, ctx.tenant_id, command.evidence.communication_id)
    source_hash = retained_original(db, row)
    if source_hash != command.source_sha256:
        raise HTTPException(409, "COMMUNICATION_SOURCE_CHANGED")
    body = row.content.get("text_body")
    evidence = command.evidence
    if (
        not isinstance(body, str)
        or body[evidence.offset : evidence.offset + len(evidence.excerpt)] != evidence.excerpt
    ):
        raise HTTPException(422, "EVIDENCE_SPAN_MISMATCH")
    choices = {choice.invoice_id: choice for choice in candidates(db, row)}
    if any(
        key not in choices or choices[key].customer_id != command.customer_id for key in command.invoice_ids
    ):
        raise HTTPException(403, "COMMUNICATION_LINK_SCOPE_DENIED")
    for key in command.invoice_ids:
        choice = choices[key]
        if choice.provenance == "explicit_reference" and not re.search(
            r"(?<![\w-])" + re.escape(choice.number) + r"(?![\w-])",
            evidence.excerpt,
        ):
            raise HTTPException(422, "INVOICE_REFERENCE_OUTSIDE_EVIDENCE")
    payload = json.dumps(command.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode()).hexdigest()
    previous = db.scalar(
        select(AuditEvent).where(
            AuditEvent.tenant_id == ctx.tenant_id,
            AuditEvent.target_id == row.id,
            AuditEvent.action == "communication.invoices_linked",
            AuditEvent.details["command_sha256"].as_string() == digest,
        )
    )
    if previous is not None:
        if previous.details.get("command") != payload:
            raise HTTPException(409, "COMMUNICATION_LINK_REPLAY_CONFLICT")
        return row.id
    if row.version != command.expected_version:
        raise HTTPException(409, "COMMUNICATION_VERSION_CONFLICT")
    for key in command.invoice_ids:
        choice = choices[key]
        db.add(
            CommunicationInvoiceLink(
                tenant_id=ctx.tenant_id,
                communication_id=row.id,
                invoice_id=key,
                claim_or_context_key=f"reviewed:{digest}",
                provenance=choice.provenance,
                confidence="contextual_unverified_sender",
                classification="reviewed_reference",
                evidence_excerpt=evidence.excerpt,
                evidence_offset=evidence.offset,
            )
        )
    row.version += 1
    db.add(
        AuditEvent(
            tenant_id=ctx.tenant_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.correlation_id,
            action="communication.invoices_linked",
            target_id=row.id,
            details={
                "command": payload,
                "command_sha256": digest,
                "sender_authority": "UNVERIFIED",
                "authority": "current_human" if isinstance(ctx, TenantContext) else "agent_action",
            },
        )
    )
    db.flush()
    return row.id
