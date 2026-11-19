"""Customer scope reviews guard future synthetic source imports until explicit disposition."""

from copy import deepcopy
from datetime import timedelta
from uuid import UUID

import pytest
from accounting_support import apply, context
from case_promise_support import communication, invoices
from identity_support import actor_client
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.features.accounting.dto import AccountingDataChanged
from aco.features.accounting.models import (
    AccountingEvent,
    CollectionRecord,
    DemoSourceRecord,
    InvoiceSnapshot,
)
from aco.features.cases.dto import CommunicationEvidence
from aco.features.identity.models import now
from aco.features.policies.guards import PolicyGuards
from aco.features.policies.models import CollectionHold
from aco.features.policies.service import PolicyService
from aco.features.promises.dto import CancelPromise, RecordPromise
from aco.features.promises.models import CustomerScopeReview, PromiseInvoiceLink
from aco.features.promises.service import PromiseService


def publish_new_invoice(db: Session, template: InvoiceSnapshot, key: str) -> AccountingDataChanged:
    """Write explicitly synthetic native source, then use the actual ingestion service."""
    source = db.scalar(
        select(DemoSourceRecord)
        .where(
            DemoSourceRecord.tenant_id == template.tenant_id,
            DemoSourceRecord.provider_connection_id == template.provider_connection_id,
            DemoSourceRecord.kind == "invoice",
            DemoSourceRecord.external_id == template.external_invoice_id,
        )
        .order_by(DemoSourceRecord.revision.desc())
    )
    assert source is not None
    payload = deepcopy(source.payload)
    payload.update(external_id=key, number=key)
    created = DemoSourceRecord(
        tenant_id=source.tenant_id,
        provider_connection_id=source.provider_connection_id,
        organisation_id=source.organisation_id,
        kind="invoice",
        external_id=key,
        revision=1,
        payload=payload,
        effective_at=now(),
        observed_at=now(),
    )
    db.add(created)
    db.commit()
    return AccountingDataChanged(
        provider_connection_id=source.provider_connection_id,
        organisation_id=source.organisation_id,
        external_invoice_id=key,
        external_event_id=f"synthetic-new-invoice:{created.sequence}",
    )


def financial_state(row: InvoiceSnapshot) -> tuple[UUID, int, int, str, str]:
    return row.id, row.original_minor, row.balance_minor, row.financial_status, row.source_revision


@pytest.mark.parametrize("deadline_passed", [False, True])
def test_unresolved_scope_review_blocks_new_import_until_authorized_cancellation(
    deadline_passed: bool,
) -> None:
    with actor_client("owner") as (client, db, user, tenant):
        existing = invoices(client, db, tenant)
        ctx = context(tenant, user.id)
        first = existing[0]
        unrelated = db.scalar(
            select(InvoiceSnapshot).where(
                InvoiceSnapshot.tenant_id == tenant,
                InvoiceSnapshot.customer_id != first.customer_id,
                InvoiceSnapshot.balance_minor > 0,
                InvoiceSnapshot.financial_status == "open",
            )
        )
        assert unrelated is not None
        dispute_id = PolicyService(db).place_hold(
            ctx, first.id, "active_dispute", "Independent invoice dispute"
        )
        db.commit()
        msg = communication(
            db, tenant, existing, "Synthetic unresolved promise: I will pay some of these invoices."
        )
        evidence = CommunicationEvidence(
            communication_id=msg.id, excerpt=str(msg.content["text_body"]), offset=0
        )
        promise = PromiseService(db).record_promise(
            ctx,
            RecordPromise(
                customer_id=first.customer_id,
                invoice_ids=[],
                candidate_invoice_ids=[r.id for r in existing],
                evidence=evidence,
                scope="ambiguous",
            ),
        )
        db.commit()
        review = db.scalar(select(CustomerScopeReview).where(CustomerScopeReview.promise_id == promise.id))
        assert review is not None and review.released_at is None
        if deadline_passed:
            # Even an elapsed review deadline is escalation metadata, never release authority.
            review.expires_at = now() - timedelta(days=1)
            db.commit()
        assert promise.invoice_links == []
        before = financial_state(first)
        notification = publish_new_invoice(db, first, "INV-NEW-SCOPE")
        result = apply(client.app, ctx, notification)
        assert result.invoice_id is not None
        db.rollback()
        imported = db.get(InvoiceSnapshot, result.invoice_id)
        assert imported is not None and imported.customer_id == first.customer_id and imported.synthetic
        imported_before = financial_state(imported)
        assert db.scalar(select(AccountingEvent).where(AccountingEvent.invoice_id == imported.id)) is not None
        projection = db.scalar(select(CollectionRecord).where(CollectionRecord.invoice_id == imported.id))
        assert projection is not None and projection.hold_reasons == []
        guard = PolicyGuards(db, now())
        blocked = guard.operational_status(ctx, imported.id)
        assert not blocked.allowed and "scope_review" in blocked.reasons
        assert guard.operational_status(ctx, unrelated.id).allowed
        assert (
            db.scalar(select(PromiseInvoiceLink).where(PromiseInvoiceLink.promise_id == promise.id)) is None
        )
        retained = db.get(InvoiceSnapshot, first.id, populate_existing=True)
        imported_current = db.get(InvoiceSnapshot, imported.id, populate_existing=True)
        assert retained is not None and imported_current is not None
        assert financial_state(retained) == before
        assert financial_state(imported_current) == imported_before
        cancelled = PromiseService(db).cancel_promise(
            ctx,
            CancelPromise(
                promise_id=promise.id,
                expected_version=promise.version,
                reason="Authorized owner rejects the ambiguous commitment after source review",
                evidence=evidence,
            ),
        )
        db.commit()
        assert cancelled.status == "CANCELLED"
        db.refresh(review)
        assert review.released_at is not None
        assert guard.operational_status(ctx, imported.id).allowed
        remaining = guard.operational_status(ctx, first.id)
        assert not remaining.allowed and "active_dispute" in remaining.reasons
        assert "scope_review" not in remaining.reasons
        hold = db.get(CollectionHold, dispute_id)
        assert hold is not None and hold.released_at is None
        assert (
            db.scalar(select(PromiseInvoiceLink).where(PromiseInvoiceLink.promise_id == promise.id)) is None
        )
        assert (
            financial_state(db.get(InvoiceSnapshot, imported.id, populate_existing=True)) == imported_before
        )
