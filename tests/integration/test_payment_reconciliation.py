"""Source-backed collection transitions and durable no-outreach continuation."""

from typing import Any, cast
from uuid import UUID, uuid4

from accounting_support import apply, publish_invoice
from case_promise_support import communication
from integration.test_policy_authority import setup_active
from policy_support import policy_world
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.features.accounting.models import CollectionRecord, InvoiceSnapshot, VerifiedPaymentAllocation
from aco.features.actions.models import ScheduledAction
from aco.features.cases.dto import UpsertCase
from aco.features.cases.service import CaseService
from aco.features.identity.models import AuditEvent, now
from aco.features.policies.models import CollectionHold


def pay(client: Any, tenant: UUID, invoice: InvoiceSnapshot) -> dict[str, Any]:
    body = {
        "provider_connection_id": str(invoice.provider_connection_id),
        "external_invoice_id": invoice.external_invoice_id,
        "operation_id": str(uuid4()),
        "amount": {
            "minor_units": invoice.balance_minor,
            "currency": invoice.currency,
            "exponent": invoice.exponent,
        },
    }
    response = client.post(f"/api/workspaces/{tenant}/demo/accounting/simulate-payment", json=body)
    assert response.status_code == 200, response.text
    assert (
        client.post(f"/api/workspaces/{tenant}/demo/accounting/simulate-payment", json=body).json()
        == response.json()
    )
    return cast(dict[str, Any], response.json())


def queued(store: Session, invoice_id: UUID) -> list[ScheduledAction]:
    return list(
        store.scalars(
            select(ScheduledAction).where(
                ScheduledAction.invoice_id == invoice_id, ScheduledAction.status == "queued"
            )
        )
    )


def test_source_paid_cancels_pending_outreach_preserves_dispute_and_source_history() -> None:
    with policy_world() as (client, store, ctx, invoices, saved):
        setup_active(client, store, ctx, saved)
        invoice = next(i for i in invoices if i.number == "INV-101")
        pending = queued(store, invoice.id)
        assert pending
        action_id, invoice_id = pending[0].id, invoice.id
        msg = communication(store, ctx.tenant_id, [invoice], "The purchase order is incorrect.")
        case = CaseService(store).upsert_case(
            ctx,
            UpsertCase.model_validate(
                {
                    "customer_id": invoice.customer_id,
                    "invoice_ids": [invoice.id],
                    "issue_type": "wrong_po",
                    "claim_key": "po-mismatch",
                    "substance": "PO dispute",
                    "evidence": {
                        "communication_id": msg.id,
                        "excerpt": "The purchase order is incorrect.",
                        "offset": 0,
                    },
                }
            ),
        )
        case_id = case.case.id
        store.commit()
        result = pay(client, ctx.tenant_id, invoice)
        store.rollback()
        store.expire_all()
        action = store.get(ScheduledAction, action_id)
        assert action and action.status == "cancelled"
        assert action.last_error == "SOURCE_PAID"
        assert not queued(store, invoice_id)
        collection = store.scalar(select(CollectionRecord).where(CollectionRecord.invoice_id == invoice_id))
        assert collection and collection.lifecycle == "closed" and "active_dispute" in collection.hold_reasons
        assert CaseService(store).get(ctx, case_id).status != "CLOSED"
        holds = list(store.scalars(select(CollectionHold).where(CollectionHold.invoice_id == invoice_id)))
        assert any(h.reason == "active_dispute" and h.released_at is None for h in holds)
        assert result["financial_status"] == "paid" and result["source_revision"] == "2"
        assert (
            len(
                list(
                    store.scalars(
                        select(VerifiedPaymentAllocation).where(
                            VerifiedPaymentAllocation.invoice_id == invoice_id
                        )
                    )
                )
            )
            == 1
        )
        audits = list(
            store.scalars(
                select(AuditEvent).where(
                    AuditEvent.tenant_id == ctx.tenant_id,
                    AuditEvent.action == "accounting.followups_reconciled",
                )
            )
        )
        assert len(audits) == 1 and audits[0].details["observation_id"]
        assert audits[0].details["cancelled_action_ids"] == str(action_id)


def test_source_correction_replaces_stale_grant_once_without_backlog() -> None:
    with policy_world() as (client, store, ctx, invoices, saved):
        setup_active(client, store, ctx, saved)
        invoice = next(i for i in invoices if i.number == "INV-101")
        old = queued(store, invoice.id)[0]
        old_id, invoice_id = old.id, invoice.id
        event = publish_invoice(
            store,
            ctx.tenant_id,
            invoice.provider_connection_id,
            invoice.external_invoice_id,
            source_status="OPEN_CORRECTED",
        )
        apply(client.app, ctx, event)
        store.rollback()
        store.expire_all()
        current = queued(store, invoice_id)
        assert len(current) == 1 and current[0].id != old_id
        assert current[0].source_revision == "2" and current[0].due_at > now()
        cancelled = store.get(ScheduledAction, old_id)
        assert cancelled is not None
        assert cancelled.status == "cancelled" and cancelled.last_error == "SOURCE_REVISION_CHANGED"
        new_id = current[0].id
        store.rollback()
        store.expire_all()
        apply(client.app, ctx, event)
        store.rollback()
        store.expire_all()
        assert [a.id for a in queued(store, invoice_id)] == [new_id]


def test_verified_reversal_reopens_one_current_action_and_preserves_cash() -> None:
    from aco.features.accounting.models import DemoSourceRecord

    with policy_world() as (client, store, ctx, invoices, saved):
        setup_active(client, store, ctx, saved)
        invoice = next(i for i in invoices if i.number == "INV-101")
        invoice_id, connection, external = (
            invoice.id,
            invoice.provider_connection_id,
            invoice.external_invoice_id,
        )
        old_id = queued(store, invoice_id)[0].id
        pay(client, ctx.tenant_id, invoice)
        store.rollback()
        store.expire_all()
        original = store.scalar(
            select(VerifiedPaymentAllocation).where(VerifiedPaymentAllocation.invoice_id == invoice_id)
        )
        assert original is not None
        event = publish_invoice(
            store,
            ctx.tenant_id,
            connection,
            external,
            status="open",
            source_status="REOPENED",
            outstanding={"minor_units": 10000, "currency": "AUD", "exponent": 2},
        )
        stamp = now()
        store.add(
            DemoSourceRecord(
                tenant_id=ctx.tenant_id,
                provider_connection_id=connection,
                organisation_id=event.organisation_id,
                kind="payment",
                external_id="REV-T18/1",
                revision=1,
                effective_at=stamp,
                observed_at=stamp,
                payload={
                    "external_payment_id": "REV-T18",
                    "external_allocation_id": "1",
                    "external_invoice_id": external,
                    "kind": "reversal",
                    "amount": {"minor_units": -10000, "currency": "AUD", "exponent": 2},
                    "parent_payment_id": original.external_payment_id,
                    "parent_allocation_id": original.external_allocation_id,
                },
            )
        )
        store.commit()
        result = apply(client.app, ctx, event)
        assert result.collection_lifecycle == "active" and result.financial_status == "open"
        store.expire_all()
        future = queued(store, invoice_id)
        assert len(future) == 1 and future[0].id != old_id and future[0].due_at > now()
        assert future[0].source_revision == "3"
        cash = list(
            store.scalars(
                select(VerifiedPaymentAllocation).where(VerifiedPaymentAllocation.invoice_id == invoice_id)
            )
        )
        assert sorted(a.minor_units for a in cash) == [-10000, 10000]
        assert next(a for a in cash if a.kind == "reversal").parent_id == original.id
        from aco.features.policies.service import PolicyService

        PolicyService(store).place_hold(ctx, invoice_id, "active_dispute", "Independent dispute remains")
        store.commit()
        apply(client.app, ctx, event)
        store.rollback()
        store.expire_all()
        assert not queued(store, invoice_id)
        collection = store.scalar(select(CollectionRecord).where(CollectionRecord.invoice_id == invoice_id))
        assert (
            collection and collection.lifecycle == "active" and collection.hold_reasons == ["active_dispute"]
        )


def test_paid_never_claims_to_recall_unknown_outreach() -> None:
    with policy_world() as (client, store, ctx, invoices, saved):
        setup_active(client, store, ctx, saved)
        invoice = next(i for i in invoices if i.number == "INV-101")
        action = queued(store, invoice.id)[0]
        action_id = action.id
        # Explicit uncertain-history fixture; no provider submission is fabricated.
        action.status = "unknown_outcome"
        store.commit()
        pay(client, ctx.tenant_id, invoice)
        store.expire_all()
        current = store.get(ScheduledAction, action_id)
        assert current and current.status == "unknown_outcome" and not current.automatic_resend
        from aco.features.policies.guards import PolicyGuards

        assert not PolicyGuards(store, now()).operational_status(ctx, invoice.id).allowed
