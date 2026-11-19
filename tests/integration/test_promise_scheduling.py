"""Genuine commitment event schedules canonical durable work without granting outreach."""

from datetime import UTC, datetime, timedelta

from accounting_support import context
from case_promise_support import communication, invoices
from identity_support import actor_client
from sqlalchemy import select

from aco.core.types import Money
from aco.features.actions.models import OutboxEvent, ScheduledAction
from aco.features.cases.dto import CommunicationEvidence
from aco.features.promises.dto import ConfirmPromise, RecordPromise
from aco.features.promises.service import PromiseService


def test_confirmed_promise_queues_exact_cutoff_once_without_accounting_trigger_or_outbound_activation() -> (
    None
):
    from aco.features.promises.scheduling import schedule_promise_checks, validate_promise_check

    with actor_client("owner") as (client, db, user, tenant):
        invoice = invoices(client, db, tenant)[0]
        msg = communication(db, tenant, [invoice], "SYNTHETIC commitment: AUD40 tomorrow.")
        evidence = CommunicationEvidence(
            communication_id=msg.id, excerpt=str(msg.content["text_body"]), offset=0
        )
        ctx = context(tenant, user.id)
        service = PromiseService(db)
        captured = service.record_promise(
            ctx,
            RecordPromise(
                customer_id=invoice.customer_id,
                invoice_ids=[invoice.id],
                evidence=evidence,
                amount=Money(minor_units=4000, currency="AUD", exponent=2),
                due_date=(datetime.now(UTC) + timedelta(days=1)).date(),
                timezone="UTC",
            ),
        )
        confirmed = service.confirm_promise(
            ctx,
            ConfirmPromise(
                promise_id=captured.id,
                expected_version=captured.version,
                confirmation_evidence=evidence,
                reason="Owner confirms this synthetic commitment only.",
            ),
        )
        db.commit()
        actions = list(db.scalars(select(ScheduledAction).where(ScheduledAction.kind == "promise_check")))
        assert len(actions) == 1
        action = actions[0]
        assert (
            action.source_event_id is None
            and action.policy_event_id is None
            and action.policy_version_id is None
        )
        assert (
            action.promise_event_id is not None
            and action.promise_installment_id == confirmed.installments[0].id
        )
        assert action.due_at == confirmed.cutoff_at
        assert action.outbound_paused and not action.automation_enabled
        assert action.reservation_id is None
        assert validate_promise_check(db, action).id == confirmed.id
        refs = schedule_promise_checks(db, ctx, confirmed.id, confirmed.version)
        assert [ref.action_id for ref in refs] == [action.id]
        assert len(list(db.scalars(select(OutboxEvent).where(OutboxEvent.action_id == action.id)))) == 1
        assert service.get(ctx, confirmed.id).scheduling_status == "QUEUED"

        from uuid import uuid4

        import pytest
        from fastapi import HTTPException

        from aco.features.identity.models import Membership
        from aco.features.promises.dto import CancelPromise
        from aco.features.promises.models import PaymentPromise

        original = dict(action.command)
        action.command = {**original, "invoice_ids": [str(invoice.id), str(uuid4())]}
        with db.no_autoflush, pytest.raises(HTTPException) as refused:
            validate_promise_check(db, action)
        assert refused.value.status_code == 403
        db.rollback()
        reloaded = db.get(ScheduledAction, action.id, populate_existing=True)
        assert reloaded is not None
        action = reloaded
        current = db.get(PaymentPromise, confirmed.id)
        assert current is not None
        current.amount = {"minor_units": 4001, "currency": "AUD", "exponent": 2}
        db.flush()
        with pytest.raises(HTTPException) as refused:
            validate_promise_check(db, action)
        assert refused.value.detail == "PROMISE_CHECK_COMMITMENT_CHANGED"
        db.rollback()
        member = db.scalar(
            select(Membership).where(Membership.tenant_id == tenant, Membership.user_id == user.id)
        )
        assert member is not None
        member.active = False
        db.flush()
        with pytest.raises(HTTPException) as refused:
            validate_promise_check(db, action)
        assert refused.value.detail == "PROMISE_CONFIRMING_AUTHORITY_REVOKED"
        db.rollback()
        cancelled = service.cancel_promise(
            ctx,
            CancelPromise(
                promise_id=confirmed.id,
                expected_version=confirmed.version,
                reason="Authorized cancellation of synthetic commitment.",
                evidence=evidence,
            ),
        )
        db.commit()
        assert cancelled.status == "CANCELLED"
        db.refresh(action)
        assert action.status == "cancelled"
