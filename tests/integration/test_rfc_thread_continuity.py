"""Actual guarded inbound persistence of scoped RFC replies; synthetic-only sources."""

import asyncio
from email import policy
from email.parser import BytesParser
from hashlib import sha256

import pytest
from ingress_support import ingress_world
from sqlalchemy import select

from aco.features.communications.models import Communication, CommunicationInvoiceLink, EmailIngressReceipt
from aco.integrations.ports.email_dto import EmailMessage, EmailSource


def reply(message: EmailMessage, references: tuple[str, ...]) -> EmailMessage:
    original = message.source.content
    assert original is not None
    mime = BytesParser(policy=policy.default).parsebytes(original)
    mime["In-Reply-To"] = references[-1]
    mime["References"] = " ".join(references)
    raw = mime.as_bytes()
    return message.model_copy(
        update={
            "provider_thread_ref": None,
            "in_reply_to": references[-1],
            "references": references,
            "source": EmailSource(
                kind="PROVIDER_ORIGINAL", content=raw, integrity_sha256=sha256(raw).hexdigest()
            ),
        }
    )


@pytest.mark.parametrize("ambiguous", [False, True])
def test_reply_without_native_thread_reuses_only_unambiguous_scoped_rfc_parent(ambiguous: bool) -> None:
    with ingress_world() as lane:
        first = lane.message(body="Synthetic source about INV-101.")
        second = lane.message(body="Synthetic independent conversation about INV-101.")
        assert first.message_id is not None and second.message_id is not None
        refs = (first.message_id, second.message_id) if ambiguous else (first.message_id,)
        incoming = reply(lane.message(body="Synthetic reply with no invoice number."), refs)
        lane.world.store.rollback()
        original = asyncio.run(lane.deliver(first))
        if ambiguous:
            asyncio.run(lane.deliver(second))
        followup = asyncio.run(lane.deliver(incoming))
        asyncio.run(lane.work())
        db = lane.world.store
        db.rollback()
        before = db.get(EmailIngressReceipt, original)
        after = db.get(EmailIngressReceipt, followup)
        assert before is not None and after is not None
        parent = db.get(Communication, before.communication_id)
        record = db.get(Communication, after.communication_id)
        assert parent is not None and record is not None
        assert record.source_state == "stored"
        links = list(
            db.scalars(
                select(CommunicationInvoiceLink).where(CommunicationInvoiceLink.communication_id == record.id)
            )
        )
        if ambiguous:
            assert record.thread_id != parent.thread_id
            assert links == []
            assert after.agent_run_id is None and after.review_required
        else:
            assert record.thread_id == parent.thread_id
            assert [row.invoice_id for row in links] == [lane.world.invoice_id]
            assert links[0].evidence_excerpt == ""


@pytest.mark.parametrize("foreign_scope", ["tenant", "connection"])
def test_rfc_parent_identity_never_crosses_tenant_or_connection(foreign_scope: str) -> None:
    from uuid import uuid4

    from aco.features.communications.linking import resolve_thread
    from aco.features.communications.models import CommunicationThread
    from aco.features.identity.models import ProviderConnection, Tenant, now

    with ingress_world() as lane:
        db = lane.world.store
        tenant_id = lane.world.ctx.tenant_id
        if foreign_scope == "tenant":
            foreign = Tenant(name="Separate synthetic RFC owner", timezone="UTC")
            db.add(foreign)
            db.flush()
            tenant_id = foreign.id
        connection = ProviderConnection(
            tenant_id=tenant_id, provider="demo_email", provider_type="email", status="READY"
        )
        db.add(connection)
        db.flush()
        thread = CommunicationThread(
            tenant_id=tenant_id, provider_connection_id=connection.id, purpose="synthetic_retained_history"
        )
        db.add(thread)
        db.flush()
        db.add(
            Communication(
                tenant_id=tenant_id,
                provider_connection_id=connection.id,
                thread_id=thread.id,
                operation_id=uuid4(),
                native_message_id="synthetic-rfc-collision",
                direction="incoming",
                kind="SYNTHETIC_FIXTURE",
                synthetic=True,
                content={"message_id": "<same-reference@synthetic.invalid>"},
                event_at=now(),
                delivery_state="received",
                source_state="synthetic_fixture",
            )
        )
        db.commit()
        incoming = reply(
            lane.message(body="Synthetic context-only reply."), ("<same-reference@synthetic.invalid>",)
        )
        resolution = resolve_thread(db, incoming)
        assert resolution.thread is None and not resolution.ambiguous
