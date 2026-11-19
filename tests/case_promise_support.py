"""Explicit synthetic communication fixtures; never verified live ingestion."""

from uuid import UUID, uuid4

from accounting_support import seed
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.communications.models import Communication, CommunicationInvoiceLink, CommunicationThread
from aco.features.identity.models import ProviderConnection, now


def invoices(client: TestClient, store: Session, tenant: UUID) -> list[InvoiceSnapshot]:
    seed(client, tenant)
    rows = list(
        store.scalars(
            select(InvoiceSnapshot)
            .where(InvoiceSnapshot.tenant_id == tenant)
            .order_by(InvoiceSnapshot.number)
        )
    )
    first = next(r for r in rows if r.number == "INV-101")
    return [first] + [r for r in rows if r.customer_id == first.customer_id and r.id != first.id]


def communication(store: Session, tenant: UUID, rows: list[InvoiceSnapshot], body: str) -> Communication:
    conn = store.scalar(
        select(ProviderConnection).where(
            ProviderConnection.tenant_id == tenant, ProviderConnection.provider_type == "email"
        )
    )
    if conn is None:
        conn = ProviderConnection(
            tenant_id=tenant, provider="fixture", provider_type="email", status="NOT_CONFIGURED"
        )
        store.add(conn)
        store.flush()
    thread = store.scalar(
        select(CommunicationThread).where(
            CommunicationThread.tenant_id == tenant,
            CommunicationThread.origin_invoice_id == rows[0].id,
            CommunicationThread.provider_connection_id == conn.id,
        )
    )
    if thread is None:
        thread = CommunicationThread(
            tenant_id=tenant,
            provider_connection_id=conn.id,
            origin_invoice_id=rows[0].id,
            customer_id=rows[0].customer_id,
            native_thread_id=None,
        )
        store.add(thread)
        store.flush()
    msg = Communication(
        tenant_id=tenant,
        provider_connection_id=conn.id,
        thread_id=thread.id,
        origin_invoice_id=rows[0].id,
        operation_id=uuid4(),
        native_message_id=None,
        direction="incoming",
        channel="email",
        kind="SYNTHETIC_FIXTURE",
        synthetic=True,
        content={
            "text_body": body,
            "sender_authority": "UNVERIFIED",
            "provenance": "explicit synthetic test fixture, not verified live ingress",
        },
        event_at=now(),
        delivery_state="received",
        source_state="synthetic_fixture",
    )
    store.add(msg)
    store.flush()
    for row in rows:
        store.add(
            CommunicationInvoiceLink(
                tenant_id=tenant,
                communication_id=msg.id,
                invoice_id=row.id,
                claim_or_context_key="fixture",
                provenance="explicit_reference",
                confidence="1",
                evidence_excerpt=body,
                evidence_offset=0,
                classification="synthetic_fixture",
            )
        )
    store.commit()
    return msg
