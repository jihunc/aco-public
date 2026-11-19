"""Actual terminal reminder failures retain history and permit only safe future reconsideration."""

from datetime import timedelta
from typing import Any

import pytest
from reminder_support import dispatch_one, reminder_world
from sqlalchemy import func, select

from aco.core.types import ProviderErrorCode, ProviderFailure
from aco.features.accounting.models import InvoiceSnapshot
from aco.features.actions.models import ActionExecution, ScheduledAction
from aco.features.communications.models import Communication, EmailDispatchBinding, EmailSubmission
from aco.features.identity.models import now
from aco.features.policies.models import ContactReservation, PolicyOccurrence
from aco.features.policies.service import PolicyService
from aco.integrations.demo_email.adapter import DemoEmailAdapter
from aco.integrations.demo_email.models import NativeEmailInvocation
from aco.integrations.ports.email import EmailFailure


def test_prebinding_cap_refusal_retains_occurrence_and_future_no_backlog_continuation() -> None:
    with reminder_world() as (client, store, ctx, invoices, saved):
        first = dispatch_one(client, store, ctx)
        assert first.status == "succeeded", first.last_error
        source = store.get(InvoiceSnapshot, first.invoice_id)
        other = store.scalar(
            select(InvoiceSnapshot)
            .join(ScheduledAction, ScheduledAction.invoice_id == InvoiceSnapshot.id)
            .where(
                InvoiceSnapshot.customer_id == source.customer_id,
                InvoiceSnapshot.id != source.id,
                ScheduledAction.status == "queued",
            )
        )
        blocked = dispatch_one(client, store, ctx, other.id)
        occurrence = store.get(PolicyOccurrence, blocked.occurrence_id, populate_existing=True)
        assert blocked.status == "failed" and blocked.last_error == "customer_contact_cap"
        assert (
            store.scalar(select(EmailDispatchBinding).where(EmailDispatchBinding.action_id == blocked.id))
            is None
        )
        assert store.scalar(select(func.count()).select_from(NativeEmailInvocation)) == 1
        assert occurrence.state == "pending_recompute"
        assert occurrence.reconsider_after is not None and occurrence.last_reason == "customer_contact_cap"
        # Current accepted contact remains capped, even though no-effect occurrence is now recoverable.
        assert PolicyService(store).recompute_next_action(ctx, blocked.invoice_id, now()) is None
        # Exercise the owning scheduler's future clock, never rewrite a financial observation.
        # The original120day rule is then past; only the verified no-outreach occurrence continues.
        future = now() + timedelta(days=150)
        next_action = PolicyService(store, future).recompute_next_action(ctx, blocked.invoice_id, future)
        assert next_action is not None
        store.flush()
        queued = list(
            store.scalars(
                select(ScheduledAction).where(
                    ScheduledAction.occurrence_id == occurrence.id, ScheduledAction.status == "queued"
                )
            )
        )
        assert len(queued) == 1 and queued[0].id != blocked.id and queued[0].due_at > future
        assert queued[0].operation_key != blocked.operation_key
        assert blocked.status == "failed" and first.status == "succeeded"
        PolicyService(store, future).recompute_next_action(ctx, blocked.invoice_id, future)
        assert (
            store.scalar(
                select(func.count())
                .select_from(ScheduledAction)
                .where(ScheduledAction.occurrence_id == occurrence.id, ScheduledAction.status == "queued")
            )
            == 1
        )
        untouched = next(i for i in invoices if i.id not in {first.invoice_id, blocked.invoice_id})
        assert PolicyService(store, future).recompute_next_action(ctx, untouched.id, future) is None
        assert store.scalar(select(func.count()).select_from(NativeEmailInvocation)) == 1


@pytest.mark.parametrize("retryable", [False, True], ids=["auth_expired", "retry_exhausted"])
def test_terminal_verified_no_effect_releases_only_after_final_attempt(
    monkeypatch: Any, retryable: Any
) -> None:
    code = ProviderErrorCode.TEMPORARILY_UNAVAILABLE if retryable else ProviderErrorCode.AUTH_EXPIRED
    calls = []

    async def refuse(adapter: Any, actor: Any, command: Any) -> Any:
        calls.append(command.operation_id)
        raise EmailFailure(ProviderFailure(code=code, retryable=retryable, may_have_side_effect=False))

    monkeypatch.setattr(DemoEmailAdapter, "send_email", refuse)
    with reminder_world() as (client, store, ctx, invoices, saved):
        action = dispatch_one(client, store, ctx)
        binding = store.scalar(
            select(EmailDispatchBinding).where(EmailDispatchBinding.action_id == action.id)
        )
        contact = store.get(ContactReservation, binding.contact_reservation_id)
        message = store.get(Communication, binding.communication_id)
        occurrence = store.get(PolicyOccurrence, action.occurrence_id)
        for _ in range(2 if retryable else 0):
            assert action.status == "queued" and contact.status == "reserved" and contact.released_at is None
            assert message.delivery_state == "prepared" and occurrence.state == "available"
            invoice_id = action.invoice_id
            action = dispatch_one(client, store, ctx, invoice_id)
            store.refresh(contact)
            store.refresh(message)
            store.refresh(occurrence)
        submission = store.scalar(select(EmailSubmission).where(EmailSubmission.binding_id == binding.id))
        assert action.status == ("dead_letter" if retryable else "failed")
        assert action.last_error == code.value and not action.automatic_resend
        assert submission.state == "no_effect" and submission.failure["may_have_side_effect"] is False
        assert len(calls) == (3 if retryable else 1)
        assert store.scalar(select(func.count()).select_from(NativeEmailInvocation)) == 0
        assert contact.status == "released" and contact.released_at is not None
        assert contact.evidence["submission_id"] == str(submission.id)
        assert message.delivery_state == "failed"
        assert occurrence.state == "pending_recompute" and occurrence.reconsider_after is not None
        old_id, old_operation = action.id, action.operation_key
        store.rollback()
        response = client.post(
            f"/api/workspaces/{ctx.tenant_id}/policies/{saved['policy_id']}/recompute",
            json={"expected_version": saved["version"]},
        )
        assert response.status_code == 200, response.text
        store.expire_all()
        queued = list(
            store.scalars(
                select(ScheduledAction).where(
                    ScheduledAction.occurrence_id == occurrence.id, ScheduledAction.status == "queued"
                )
            )
        )
        assert len(queued) == 1 and queued[0].id != old_id and queued[0].due_at > now()
        assert queued[0].operation_key != old_operation
        assert store.get(ScheduledAction, old_id).status == ("dead_letter" if retryable else "failed")
        assert store.scalar(
            select(func.count()).select_from(ActionExecution).where(ActionExecution.action_id == old_id)
        ) == len(calls)
        assert store.get(Communication, message.id).delivery_state == "failed"
        assert store.scalar(select(func.count()).select_from(NativeEmailInvocation)) == 0


def test_failure_finalization_rejects_stale_scope_before_mutation_and_keeps_current_send(
    monkeypatch: Any,
) -> None:
    from uuid import uuid4

    from fastapi import HTTPException

    from aco.features.actions.service import ActionLease
    from aco.features.communications.outbound import ReminderDispatcher

    original = ReminderDispatcher.archive
    observed = []

    async def inspect_after_archive(dispatcher: Any, worker: Any, binding_id: Any, grant_id: Any) -> Any:
        await original(dispatcher, worker, binding_id, grant_id)
        async with dispatcher.sessions() as db:
            action = await db.get(ScheduledAction, worker.action_id)
            lease = ActionLease(
                action_id=action.id, version=action.version, token=action.lease_token, attempt=action.attempts
            )
        failure = ProviderFailure(
            code=ProviderErrorCode.AUTH_EXPIRED, retryable=False, may_have_side_effect=False
        )
        for change in (
            {"lease_token": uuid4()},
            {"actor_id": uuid4()},
            {"action_version": worker.action_version + 1},
            {"job_id": worker.job_id + 1},
            {"tenant_id": uuid4()},
        ):
            with pytest.raises(HTTPException):
                await dispatcher._finish_failure(
                    worker.model_copy(update=change), lease, reason="AUTH_EXPIRED", failure=failure
                )
            observed.append(change)
        incarnation = dispatcher.incarnation
        dispatcher.incarnation = uuid4()
        try:
            with pytest.raises(HTTPException):
                await dispatcher._finish_failure(worker, lease, reason="AUTH_EXPIRED", failure=failure)
        finally:
            dispatcher.incarnation = incarnation
        async with dispatcher.sessions() as db:
            action = await db.get(ScheduledAction, worker.action_id)
            binding = await db.get(EmailDispatchBinding, binding_id)
            contact = await db.get(ContactReservation, binding.contact_reservation_id)
            message = await db.get(Communication, binding.communication_id)
            occurrence = await db.get(PolicyOccurrence, action.occurrence_id)
            assert action.status == "executing" and contact.status == "reserved"
            assert contact.released_at is None and message.delivery_state == "prepared"
            assert occurrence.state == "available"

    monkeypatch.setattr(ReminderDispatcher, "archive", inspect_after_archive)
    with reminder_world() as (client, store, ctx, invoices, saved):
        action = dispatch_one(client, store, ctx)
        assert len(observed) == 5
        assert action.status == "succeeded", action.last_error
        assert store.scalar(select(func.count()).select_from(NativeEmailInvocation)) == 1


def test_pending_submission_overrides_optimistic_error_and_keeps_exposure(monkeypatch: Any) -> None:
    from aco.features.usage.email import ObservedEmail

    # Real independent intent and native acceptance precede this injected outcome-write failure.
    # Its optimistic exception cannot override the stored pending submission fence.
    async def lost_outcome(*args: Any, **kwargs: Any) -> Any:
        raise EmailFailure(
            ProviderFailure(code=ProviderErrorCode.AUTH_EXPIRED, retryable=False, may_have_side_effect=False)
        )

    monkeypatch.setattr(ObservedEmail, "finish", lost_outcome)
    with reminder_world() as (client, store, ctx, invoices, saved):
        action = dispatch_one(client, store, ctx)
        binding = store.scalar(
            select(EmailDispatchBinding).where(EmailDispatchBinding.action_id == action.id)
        )
        submission = store.scalar(select(EmailSubmission).where(EmailSubmission.binding_id == binding.id))
        contact = store.get(ContactReservation, binding.contact_reservation_id)
        message = store.get(Communication, binding.communication_id)
        occurrence = store.get(PolicyOccurrence, action.occurrence_id)
        assert action.status == "unknown_outcome" and not action.automatic_resend
        assert submission.state == "pending" and submission.intent_at is not None
        assert contact.status == "unknown_outcome" and contact.released_at is None
        assert message.delivery_state == "unknown_outcome" and occurrence.state == "available"
        assert store.scalar(select(func.count()).select_from(NativeEmailInvocation)) == 1
        history = set(
            store.scalars(select(ScheduledAction.id).where(ScheduledAction.occurrence_id == occurrence.id))
        )
        assert action.id in history
        assert PolicyService(store).recompute_next_action(ctx, action.invoice_id, now()) is None
        assert (
            set(
                store.scalars(
                    select(ScheduledAction.id).where(ScheduledAction.occurrence_id == occurrence.id)
                )
            )
            == history
        )
