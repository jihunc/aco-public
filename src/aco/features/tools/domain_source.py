"""Reuse one actually read incoming original without widening selected invoice scope."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.agent.context import selected_invoices
from aco.features.agent.models import AgentRun, AgentStep
from aco.features.cases.dto import CommunicationEvidence
from aco.features.communications.models import Communication, CommunicationInvoiceLink, CommunicationThread


def can_reuse_original(
    db: Session, run: AgentRun, step: AgentStep, primary: InvoiceSnapshot, evidence: CommunicationEvidence
) -> bool:
    selected = set(selected_invoices(db, run))
    if (
        step.tenant_id != run.tenant_id
        or step.run_id != run.id
        or step.kind != "read"
        or step.status != "completed"
        or step.tool_name != "get_communication_history"
        or step.invoice_id not in selected
        or primary.id not in selected
        or primary.tenant_id != run.tenant_id
        or not primary.verified
    ):
        return False
    observed = db.get(InvoiceSnapshot, step.invoice_id, populate_existing=True)
    message = db.get(Communication, evidence.communication_id, populate_existing=True)
    if (
        observed is None
        or observed.tenant_id != run.tenant_id
        or not observed.verified
        or observed.customer_id != primary.customer_id
        or observed.provider_connection_id != primary.provider_connection_id
        or message is None
        or message.tenant_id != run.tenant_id
        or message.direction != "incoming"
        or message.source_state != "stored"
    ):
        return False
    thread = db.get(CommunicationThread, message.thread_id, populate_existing=True)
    if thread is None or thread.tenant_id != run.tenant_id or thread.customer_id != primary.customer_id:
        return False
    body = message.content.get("text_body")
    if (
        not isinstance(body, str)
        or body[evidence.offset : evidence.offset + len(evidence.excerpt)] != evidence.excerpt
    ):
        return False
    links = list(
        db.scalars(
            select(CommunicationInvoiceLink)
            .where(
                CommunicationInvoiceLink.tenant_id == run.tenant_id,
                CommunicationInvoiceLink.communication_id == message.id,
            )
            .execution_options(populate_existing=True)
        )
    )
    if not {primary.id, observed.id}.issubset({link.invoice_id for link in links}):
        return False
    for link in links:
        invoice = db.get(InvoiceSnapshot, link.invoice_id, populate_existing=True)
        if (
            link.confidence not in {"1", "contextual_unverified_sender"}
            or (
                link.confidence == "contextual_unverified_sender"
                and link.provenance not in {"origin_thread", "explicit_reference", "contextual_inference"}
            )
            or invoice is None
            or invoice.tenant_id != run.tenant_id
            or not invoice.verified
            or invoice.customer_id != primary.customer_id
            or invoice.provider_connection_id != primary.provider_connection_id
        ):
            return False
    return True
