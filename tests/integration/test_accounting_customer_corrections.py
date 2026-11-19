"""Current source-customer corrections retain historical originals without transferring authority."""

from uuid import uuid4

import pytest
from accounting_support import apply, publish_invoice
from action_support import action_world
from case_promise_support import communication
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from aco.features.accounting.models import Customer, InvoiceSnapshot
from aco.features.cases.dto import CommunicationEvidence
from aco.features.cases.service import scope_invoices, validate_evidence
from aco.features.communications.models import CommunicationThread


def test_canonical_customer_correction_preserves_original_thread_and_current_scope() -> None:
    with action_world() as world:
        db = world.store
        first = db.get_one(InvoiceSnapshot, world.invoice_id)
        other = db.scalar(
            select(InvoiceSnapshot).where(
                InvoiceSnapshot.tenant_id == world.ctx.tenant_id,
                InvoiceSnapshot.customer_id != first.customer_id,
            )
        )
        assert other is not None
        old_customer, new_customer = first.customer_id, other.customer_id
        new_external = db.get_one(Customer, new_customer).external_customer_id
        financial = (
            first.original_minor,
            first.balance_minor,
            first.currency,
            first.exponent,
            first.financial_status,
        )
        msg = communication(db, world.ctx.tenant_id, [first], "Retained synthetic source about this invoice.")
        message_id, thread_id, invoice_id = msg.id, msg.thread_id, first.id
        before = db.scalar(
            text("SELECT to_jsonb(t) FROM communication_threads t WHERE id=:id"), {"id": thread_id}
        )
        event = publish_invoice(
            db, world.ctx.tenant_id, world.connection_id, external_customer_id=new_external
        )
        db.rollback()
        result = apply(world.client.app, world.ctx, event)
        db.expire_all()
        current = db.get_one(InvoiceSnapshot, invoice_id)
        assert current.customer_id == new_customer
        assert (
            current.original_minor,
            current.balance_minor,
            current.currency,
            current.exponent,
            current.financial_status,
        ) == financial
        assert result.invoice_id == invoice_id
        assert (
            db.scalar(text("SELECT to_jsonb(t) FROM communication_threads t WHERE id=:id"), {"id": thread_id})
            == before
        )
        retained = db.get_one(CommunicationThread, thread_id)
        assert retained.customer_id == old_customer
        assert scope_invoices(db, world.ctx.tenant_id, new_customer, [invoice_id])[0].id == invoice_id
        with pytest.raises(HTTPException, match="INVOICE_SCOPE_MISMATCH"):
            scope_invoices(db, world.ctx.tenant_id, old_customer, [invoice_id])
        with pytest.raises(HTTPException, match="EVIDENCE_CUSTOMER_MISMATCH"):
            validate_evidence(
                db,
                world.ctx.tenant_id,
                new_customer,
                CommunicationEvidence(communication_id=message_id, excerpt="Retained", offset=0),
                [invoice_id],
            )
        rows = db.execute(
            text(
                "SELECT customer_id, provider_connection_id, verified_at_capture "
                "FROM invoice_customer_associations "
                "WHERE tenant_id=:tenant AND invoice_id=:invoice"
            ),
            {"tenant": world.ctx.tenant_id, "invoice": invoice_id},
        ).all()
        assert {row.customer_id for row in rows} == {old_customer, new_customer}
        assert all(
            row.provider_connection_id == world.connection_id and row.verified_at_capture for row in rows
        )
        for statement in (
            "UPDATE invoice_customer_associations SET source_revision='forged' WHERE invoice_id=:invoice",
            "DELETE FROM invoice_customer_associations WHERE invoice_id=:invoice",
        ):
            with (
                pytest.raises(DBAPIError, match="invoice customer association history is immutable"),
                db.begin_nested(),
            ):
                db.execute(text(statement), {"invoice": invoice_id})
        # A customer never observed for this invoice cannot become historical authority.
        wrong = db.scalar(
            select(Customer).where(
                Customer.tenant_id == world.ctx.tenant_id, Customer.id.not_in([old_customer, new_customer])
            )
        )
        assert wrong is not None
        with pytest.raises(DBAPIError), db.begin_nested():
            db.add(
                CommunicationThread(
                    tenant_id=world.ctx.tenant_id,
                    provider_connection_id=retained.provider_connection_id,
                    origin_invoice_id=invoice_id,
                    customer_id=wrong.id,
                    native_thread_id="forged-" + uuid4().hex,
                )
            )
            db.flush()
        from alembic.config import Config
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from alembic.script import ScriptDirectory

        retained_rows = (
            db.execute(
                text(
                    "SELECT to_jsonb(a) FROM invoice_customer_associations a "
                    "ORDER BY tenant_id,invoice_id,customer_id"
                )
            )
            .scalars()
            .all()
        )
        db.rollback()
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", world.url.replace("%", "%%"))
        current_head = db.scalar(text("SELECT version_num FROM alembic_version"))
        revision = ScriptDirectory.from_config(config).get_revision("r0029_invoice_customer_scope")
        assert revision is not None
        # Exercise the exact r29 guard without bypassing newer guarded migrations.
        # Its refusal must precede any DDL, preserving the actual current head/history.
        with (
            pytest.raises(DBAPIError, match="retained invoice customer history prevents lossy downgrade"),
            db.begin_nested(),
            Operations.context(MigrationContext.configure(db.connection())),
        ):
            revision.module.downgrade()
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == current_head
        assert (
            db.execute(
                text(
                    "SELECT to_jsonb(a) FROM invoice_customer_associations a "
                    "ORDER BY tenant_id,invoice_id,customer_id"
                )
            )
            .scalars()
            .all()
            == retained_rows
        )


def test_changed_customer_thread_reply_requires_review_before_agent_processing() -> None:
    import asyncio
    from email import policy
    from email.parser import BytesParser
    from hashlib import sha256

    from ingress_support import ingress_world

    from aco.features.communications.models import (
        Communication,
        CommunicationInvoiceLink,
        EmailIngressReceipt,
    )
    from aco.integrations.ports.email_dto import EmailSource

    with ingress_world() as lane:
        world, db = lane.world, lane.world.store
        first = lane.message(thread="retained-before-customer-correction")
        receipt_id = asyncio.run(lane.deliver(first))
        asyncio.run(lane.work())
        db.rollback()
        original_receipt = db.get_one(EmailIngressReceipt, receipt_id)
        original = db.get_one(Communication, original_receipt.communication_id)
        thread_id, original_id = original.thread_id, original.id
        invoice = db.get_one(InvoiceSnapshot, world.invoice_id)
        new = db.scalar(
            select(InvoiceSnapshot).where(
                InvoiceSnapshot.tenant_id == world.ctx.tenant_id,
                InvoiceSnapshot.customer_id != invoice.customer_id,
            )
        )
        assert new is not None
        new_external = db.get_one(Customer, new.customer_id).external_customer_id
        event = publish_invoice(
            db, world.ctx.tenant_id, world.connection_id, external_customer_id=new_external
        )
        db.rollback()
        apply(world.client.app, world.ctx, event)
        incoming = lane.message(thread="retained-before-customer-correction", body="Thanks for your message.")
        assert incoming.source.content is not None and first.message_id is not None
        mime = BytesParser(policy=policy.default).parsebytes(incoming.source.content)
        mime.replace_header("From", first.sender)
        mime["In-Reply-To"], mime["References"] = first.message_id, first.message_id
        raw = mime.as_bytes()
        incoming = incoming.model_copy(
            update={
                "sender": first.sender,
                "in_reply_to": first.message_id,
                "references": (first.message_id,),
                "headers": tuple(mime.items()),
                "source": EmailSource(
                    kind="PROVIDER_ORIGINAL", content=raw, integrity_sha256=sha256(raw).hexdigest()
                ),
            }
        )
        db.rollback()
        after_id = asyncio.run(lane.deliver(incoming))
        asyncio.run(lane.work())
        db.rollback()
        after = db.get_one(EmailIngressReceipt, after_id)
        saved = db.get_one(Communication, after.communication_id)
        assert after.review_required and after.agent_run_id is None
        assert saved.source_state == "stored" and saved.thread_id == thread_id
        assert (
            list(
                db.scalars(
                    select(CommunicationInvoiceLink).where(
                        CommunicationInvoiceLink.communication_id == saved.id
                    )
                )
            )
            == []
        )
        assert db.get_one(Communication, original_id).thread_id == thread_id


def test_unverified_projection_association_is_flagged_without_financial_authority() -> None:
    from aco.features.identity.models import now
    from aco.features.policies.guards import PolicyGuards

    with action_world() as world:
        db = world.store
        original = db.get_one(InvoiceSnapshot, world.invoice_id)
        values = {column.name: getattr(original, column.name) for column in InvoiceSnapshot.__table__.columns}
        values.update(
            id=uuid4(),
            external_invoice_id="UNCERTAIN-RELATIONSHIP",
            number="UNCERTAIN-RELATIONSHIP",
            verified=False,
            financial_status="unknown",
            source_revision="unknown",
        )
        uncertain = InvoiceSnapshot(**values)
        db.add(uncertain)
        db.commit()
        row = db.execute(
            text(
                "SELECT verified_at_capture,capture_kind FROM invoice_customer_associations "
                "WHERE tenant_id=:tenant AND invoice_id=:invoice"
            ),
            {"tenant": world.ctx.tenant_id, "invoice": uncertain.id},
        ).one()
        assert row.verified_at_capture is False and row.capture_kind == "current_projection"
        decision = PolicyGuards(db, now()).operational_status(world.ctx, uncertain.id)
        assert not decision.allowed and "source_balance_ineligible" in decision.reasons


def test_unobserved_supporting_association_cannot_authorize_thread() -> None:
    from aco.features.accounting.models import InvoiceCustomerAssociation

    with action_world() as world:
        db = world.store
        invoice = db.get_one(InvoiceSnapshot, world.invoice_id)
        wrong = db.scalar(
            select(Customer).where(
                Customer.tenant_id == world.ctx.tenant_id,
                Customer.provider_connection_id == world.connection_id,
                Customer.id != invoice.customer_id,
            )
        )
        assert wrong is not None
        with pytest.raises(DBAPIError, match="association requires current snapshot"), db.begin_nested():
            db.add(
                InvoiceCustomerAssociation(
                    tenant_id=world.ctx.tenant_id,
                    invoice_id=invoice.id,
                    customer_id=wrong.id,
                    provider_connection_id=world.connection_id,
                    source_revision=invoice.source_revision,
                    verified_at_capture=invoice.verified,
                    capture_kind="current_projection",
                )
            )
            db.flush()
