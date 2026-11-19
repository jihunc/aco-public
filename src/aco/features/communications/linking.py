"""Conservative source-backed invoice links; transport authentication is not sender authority."""

import re
from dataclasses import dataclass
from uuid import UUID

import nh3
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from aco.features.accounting.models import CustomerContact, InvoiceSnapshot
from aco.features.communications.models import (
    Communication,
    CommunicationInvoiceLink,
    CommunicationRfcIdentity,
    CommunicationThread,
)
from aco.integrations.ports.email_dto import EmailMessage


def sanitized_html(value: str | None) -> str | None:
    if value is None:
        return None
    return nh3.clean(
        value,
        tags={"p", "br", "b", "strong", "em", "i", "ul", "ol", "li", "blockquote"},
        attributes={},
        clean_content_tags={"script", "style", "iframe", "object", "svg"},
    )


@dataclass(frozen=True)
class InvoiceEvidence:
    invoice_id: UUID
    customer_id: UUID
    excerpt: str
    offset: int
    provenance: str


@dataclass(frozen=True)
class ThreadResolution:
    thread: CommunicationThread | None
    ambiguous: bool = False


def rfc_parents(db: Session, message: EmailMessage) -> list[Communication]:
    """One scoped union of original caller IDs and retained provider observations."""
    refs = set(message.references)
    if message.in_reply_to:
        refs.add(message.in_reply_to)
    if not refs:
        return []
    aliases = select(CommunicationRfcIdentity.communication_id).where(
        CommunicationRfcIdentity.tenant_id == message.tenant_id,
        CommunicationRfcIdentity.provider_connection_id == message.provider_connection_id,
        CommunicationRfcIdentity.rfc_message_id.in_(refs),
    )
    return list(
        db.scalars(
            select(Communication).where(
                Communication.tenant_id == message.tenant_id,
                Communication.provider_connection_id == message.provider_connection_id,
                or_(Communication.content["message_id"].as_string().in_(refs), Communication.id.in_(aliases)),
            )
        )
    )


def resolve_thread(db: Session, message: EmailMessage) -> ThreadResolution:
    """Transport/RFC identities select only one existing tenant+connection thread.

    References are context, never sender authority. Conflicting retained parents
    remain unmerged and require review, even when they belong to one customer.
    """
    matches: dict[UUID, CommunicationThread] = {}
    if message.provider_thread_ref:
        native = db.scalar(
            select(CommunicationThread).where(
                CommunicationThread.tenant_id == message.tenant_id,
                CommunicationThread.provider_connection_id == message.provider_connection_id,
                CommunicationThread.native_thread_id == message.provider_thread_ref.external_id,
            )
        )
        if native is not None:
            matches[native.id] = native
    for parent in rfc_parents(db, message):
        thread = db.get(CommunicationThread, parent.thread_id)
        if thread is not None and (
            thread.tenant_id == message.tenant_id
            and thread.provider_connection_id == message.provider_connection_id
        ):
            matches[thread.id] = thread
    if len(matches) > 1:
        return ThreadResolution(None, ambiguous=True)
    return ThreadResolution(next(iter(matches.values()), None))


def invoice_evidence(db: Session, message: EmailMessage) -> list[InvoiceEvidence]:
    """Only exact mentions within one known customer or retained RFC reply context.

    Matching an imported contact is a contextual association, never sender verification.
    Cross-customer/duplicate-number ambiguity yields no inferred financial allocation.
    """
    resolution = resolve_thread(db, message)
    if resolution.ambiguous:
        return []
    source = message.text_body or ""
    contacts = list(
        db.scalars(
            select(CustomerContact).where(
                CustomerContact.tenant_id == message.tenant_id,
                CustomerContact.email == str(message.sender),
            )
        )
    )
    customers = {row.customer_id for row in contacts}
    # Native/RFC context must itself be tenant+connection scoped and unambiguous.
    refs = set(message.references)
    if message.in_reply_to:
        refs.add(message.in_reply_to)
    contextual: dict[UUID, InvoiceEvidence] = {}
    retained_thread = resolution.thread
    if retained_thread and retained_thread.customer_id:
        customers.add(retained_thread.customer_id)
        if retained_thread.origin_invoice_id:
            contextual[retained_thread.origin_invoice_id] = InvoiceEvidence(
                retained_thread.origin_invoice_id, retained_thread.customer_id, "", 0, "origin_thread"
            )
    if refs:
        for row in rfc_parents(db, message):
            thread = db.get(CommunicationThread, row.thread_id)
            if thread and thread.customer_id:
                customers.add(thread.customer_id)
                for link in db.scalars(
                    select(CommunicationInvoiceLink).where(
                        CommunicationInvoiceLink.tenant_id == message.tenant_id,
                        CommunicationInvoiceLink.communication_id == row.id,
                    )
                ):
                    contextual.setdefault(
                        link.invoice_id,
                        InvoiceEvidence(link.invoice_id, thread.customer_id, "", 0, "contextual_inference"),
                    )
    if len(customers) != 1:
        return []
    customer = next(iter(customers))
    all_invoices = list(
        db.scalars(
            select(InvoiceSnapshot).where(
                InvoiceSnapshot.tenant_id == message.tenant_id,
            )
        )
    )
    current_by_id = {invoice.id: invoice for invoice in all_invoices}
    if any(
        row.invoice_id not in current_by_id or current_by_id[row.invoice_id].customer_id != row.customer_id
        for row in contextual.values()
    ):
        # Retain the old thread/original, but a source customer correction makes
        # current invoice attribution review-required before any automatic AI work.
        return []
    matches = dict(contextual)
    for invoice in all_invoices:
        match = re.search(r"(?<![\w-])" + re.escape(invoice.number) + r"(?![\w-])", source)
        if match:
            if invoice.customer_id != customer or sum(i.number == invoice.number for i in all_invoices) != 1:
                return []
            provenance = (
                "origin_thread"
                if invoice.id in contextual and contextual[invoice.id].provenance == "origin_thread"
                else "explicit_reference"
            )
            matches[invoice.id] = InvoiceEvidence(
                invoice.id, customer, match.group(), match.start(), provenance
            )
    # Retained context never attributes an invented excerpt to the new body.
    return list(matches.values())
