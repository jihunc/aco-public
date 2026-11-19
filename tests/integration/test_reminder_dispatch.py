"""Production HTTP, migrated PostgreSQL, worker and durable simulated email evidence."""

import json
from pathlib import Path
from typing import Any

import pytest
from boundary_evidence import BoundaryObserver
from policy_support import policy_world


def test_reviewed_synthetic_execution_is_persisted_and_never_sends_on_read() -> None:
    # Removing persisted Owner review or making GET perform outreach must fail this test.
    with policy_world() as (client, store, ctx, invoices, saved):
        response = client.get(f"/api/workspaces/{ctx.tenant_id}/communications")
        assert response.status_code == 200, response.text
        assert response.json()["items"] == []
        response = client.get(f"/api/workspaces/{ctx.tenant_id}/email-execution")
        assert response.status_code == 200, response.text
        assert response.json()["activation"] is None
        assert response.json()["system_enabled"] is False


def test_eligible_invoice_sends_once_and_saves_original(monkeypatch: Any) -> None:
    from uuid import uuid4

    from fastapi import HTTPException
    from reminder_support import reminder_world

    from aco.features.actions.service import ActionService

    original_complete = ActionService.complete_action

    def checked_complete(service: Any, worker: Any, lease: Any, result: Any) -> Any:
        if result.provider_ref is not None:
            for reference in (
                result.provider_ref.model_copy(update={"connection_id": uuid4()}),
                result.provider_ref.model_copy(update={"external_id": "wrong-message"}),
            ):
                with pytest.raises(HTTPException, match="UNTRUSTED_PROVIDER_RESULT"):
                    original_complete(
                        service, worker, lease, result.model_copy(update={"provider_ref": reference})
                    )
        return original_complete(service, worker, lease, result)

    monkeypatch.setattr(ActionService, "complete_action", checked_complete)

    with reminder_world() as (client, store, ctx, invoices, saved):
        response = client.get(f"/api/workspaces/{ctx.tenant_id}/email-execution")
        assert response.status_code == 200
        assert response.json()["activation"]["current"] is True
        assert response.json()["system_enabled"] is True
        from reminder_support import dispatch_one
        from sqlalchemy import func, select

        from aco.features.communications.models import Communication, CommunicationSource
        from aco.integrations.demo_email.models import NativeEmailInvocation, NativeEmailOperation

        action = dispatch_one(client, store, ctx)
        assert action.status == "succeeded", action.last_error
        message = store.scalar(
            select(Communication).where(Communication.origin_invoice_id == action.invoice_id)
        )
        assert message and message.direction == "outgoing"
        assert message.delivery_state == "accepted"
        assert store.scalar(select(func.count()).select_from(NativeEmailInvocation)) == 1
        assert store.scalar(select(func.count()).select_from(NativeEmailOperation)) == 1
        source = store.scalar(
            select(CommunicationSource).where(CommunicationSource.communication_id == message.id)
        )
        assert source and source.representation == "ACO_COMPOSED"
        response = client.get(f"/api/workspaces/{ctx.tenant_id}/artifacts/{source.artifact_id}/content")
        assert response.status_code == 200
        from email import message_from_bytes

        mime = message_from_bytes(response.content)
        assert mime["Message-ID"] == message.content["message_id"]
        assert mime["To"] == message.content["to"][0]
        assert (
            client.get(f"/api/workspaces/{ctx.tenant_id}/scheduled-actions?kind=invalid").status_code == 422
        )
        assert (
            client.get(f"/api/workspaces/{ctx.tenant_id}/scheduled-actions?status=invalid").status_code == 422
        )
        actions = client.get(
            f"/api/workspaces/{ctx.tenant_id}/scheduled-actions?kind=policy_due&status=succeeded"
        ).json()
        shown = next(item for item in actions if item["id"] == str(action.id))
        assert shown["kind"] == "policy_due" and shown["invoice_id"] == str(action.invoice_id)
        listed = client.get(f"/api/workspaces/{ctx.tenant_id}/communications").json()
        assert listed["items"][0]["id"] == str(message.id)
        other = client.post("/api/workspaces", json={"name": "Second synthetic scope", "timezone": "UTC"})
        assert other.status_code == 201, other.text
        foreign_tenant = other.json()["id"]
        assert client.get(f"/api/workspaces/{foreign_tenant}/communications/{message.id}").status_code == 404
        assert (
            client.get(f"/api/workspaces/{foreign_tenant}/artifacts/{source.artifact_id}/content").status_code
            == 404
        )
        assert (
            client.post(
                f"/api/workspaces/{foreign_tenant}/scheduled-actions/{action.id}/reconcile-delivery"
            ).status_code
            == 404
        )
        from uuid import UUID

        from sqlalchemy.exc import IntegrityError

        from aco.features.communications.models import CommunicationInvoiceLink

        with pytest.raises(IntegrityError), store.begin_nested():
            store.add(
                CommunicationInvoiceLink(
                    tenant_id=UUID(foreign_tenant),
                    communication_id=message.id,
                    invoice_id=action.invoice_id,
                    claim_or_context_key="foreign",
                    provenance="origin_thread",
                    confidence="1",
                    evidence_excerpt="forbidden cross-scope link",
                    evidence_offset=0,
                    classification="test",
                )
            )
            store.flush()
        store.rollback()
        assert store.scalar(select(func.count()).select_from(NativeEmailInvocation)) == 1


TICKET_CASES = [
    case
    for case in json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]
    if case["domain"] == "ticket"
]


@pytest.mark.parametrize("case", TICKET_CASES, ids=lambda case: case["id"])
def test_dispatch_ticket_boundary_cases(
    case: Any, monkeypatch: Any, boundary_observer: BoundaryObserver
) -> None:
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from fastapi import HTTPException
    from reminder_support import dispatch_maintained, reminder_world
    from sqlalchemy import func, select

    from aco.core.types import ActionResult, ActionStatus, Money
    from aco.features.accounting.models import (
        AccountingEvent,
        CustomerContact,
        InvoiceSnapshot,
    )
    from aco.features.actions.models import ScheduledAction
    from aco.features.communications.outbound import ReminderDispatcher
    from aco.features.policies.guards import PolicyGuards
    from aco.features.policies.models import CollectionPolicy, DispatchTicketRecord, RecipientControl
    from aco.integrations.demo_email.models import NativeEmailInvocation, NativeEmailOperation

    observed: dict[str, Any] = {}
    original = ReminderDispatcher.dispatch_email
    with reminder_world() as (client, store, human, invoices, saved):
        # Establish current valid recipient time through the same reviewed API;
        # the old fixed UTC+3 fixture ceased being eligible outside office hours.
        offset = 12 - datetime.now(UTC).hour
        zone = f"Etc/GMT{-offset:+d}" if offset else "Etc/GMT"
        for contact in store.scalars(
            select(CustomerContact).where(CustomerContact.tenant_id == human.tenant_id)
        ):
            control = store.scalar(select(RecipientControl).where(RecipientControl.contact_id == contact.id))
            assert control is not None
            reviewed = client.put(
                f"/api/workspaces/{human.tenant_id}/policy-recipients/{contact.id}",
                json={
                    "expected_version": control.version,
                    "expected_contact_revision": contact.source_revision,
                    "expected_destination": contact.email,
                    "timezone": zone,
                    "timezone_verified": True,
                    "owner_confirmed_tenant_timezone": False,
                    "destination_verified": True,
                    "allowed_channels": ["email"],
                    "opt_out": False,
                    "evidence": "Owner reviewed synthetic noon-zone for actual dispatch-ticket fixture",
                },
            )
            assert reviewed.status_code == 200, reviewed.text
        store.rollback()

        async def boundary(
            dispatcher: Any, db: Any, worker: Any, command: Any, ticket: Any, proposal: Any
        ) -> Any:
            row = await db.get(DispatchTicketRecord, ticket.id)
            assert row and row.consumed_at is None  # Real production issuance, never a seeded ticket.
            inputs = case["input"]
            current = row.issued_at + timedelta(seconds=inputs["age_seconds"])
            incarnation = (
                uuid4()
                if inputs["issued_worker_generation"] != inputs["current_worker_generation"]
                else dispatcher.incarnation
            )
            dispatcher.guard = lambda session: PolicyGuards(
                session,
                current,
                incarnation,
                settings=dispatcher.settings,
                email_runtime=dispatcher.email_runtime,
            )
            action = await db.get(ScheduledAction, proposal.action_id)
            invoice = await db.get(InvoiceSnapshot, action.invoice_id)
            changed = inputs["changed_versions"]
            if "source" in changed:
                from aco.features.accounting.dto import DemoPaymentCommand
                from aco.features.accounting.seed import simulate_payment

                await simulate_payment(
                    db,
                    human,
                    DemoPaymentCommand(
                        provider_connection_id=action.provider_connection_id,
                        external_invoice_id=invoice.external_invoice_id,
                        amount=Money(minor_units=1, currency=invoice.currency, exponent=invoice.exponent),
                        operation_id=uuid4(),
                    ),
                    "local",
                )
            if "policy" in changed:
                policy = await db.get(CollectionPolicy, __import__("uuid").UUID(saved["policy_id"]))
                policy.version += 1
            if "recipient" in changed:
                contact = await db.scalar(
                    select(CustomerContact).where(
                        CustomerContact.tenant_id == human.tenant_id,
                        CustomerContact.customer_id == invoice.customer_id,
                    )
                )
                control = await db.scalar(
                    select(RecipientControl).where(RecipientControl.contact_id == contact.id)
                )
                control.version += 1
            if "hold" in changed:
                from aco.features.policies.service import PolicyService

                await db.run_sync(
                    lambda session: PolicyService(session).place_hold(
                        human, invoice.id, "manual_hold", "Actual pending investigation after ticket issue"
                    )
                )
            if "approval" in changed:
                from aco.features.review.internal_work import ApprovalProposal, InternalWorkService

                event = await db.scalar(
                    select(AccountingEvent).where(AccountingEvent.invoice_id == invoice.id)
                )
                ref = await db.run_sync(
                    lambda session: InternalWorkService(session).request_approval(
                        human,
                        ApprovalProposal(
                            action_id=action.id,
                            action_version=action.version,
                            content="New AI draft requiring review",
                            destination=proposal.destination,
                            channel="email",
                            invoice_ids=(invoice.id,),
                            evidence_ids=(event.id,),
                            policy_version_id=action.policy_version_id,
                            cost_bound="0",
                            currency="AUD",
                            consent_mode="no_capture",
                        ),
                    )
                )
                proposal = proposal.model_copy(update={"approval_id": ref.request_id})
            await db.flush()
            if inputs["used"]:
                await original(dispatcher, db, worker, command, ticket, proposal)
            async with client.app.state.business_sessions() as evidence:
                before = await evidence.scalar(select(func.count()).select_from(NativeEmailInvocation))
            try:
                result = await original(dispatcher, db, worker, command, ticket, proposal)
                observed["allowed"] = True
            except HTTPException as error:
                observed["allowed"] = False
                observed["guard_reason"] = str(error.detail)
                result = ActionResult(
                    action_id=action.id, status=ActionStatus.FAILED, error_code=str(error.detail)
                )
            async with client.app.state.business_sessions() as evidence:
                after = await evidence.scalar(select(func.count()).select_from(NativeEmailInvocation))
                effects = await evidence.scalar(select(func.count()).select_from(NativeEmailOperation))
            observed["additional_provider_submissions"] = after - before
            observed["existing_effect_count"] = effects
            observed["reevaluation_required"] = not observed["allowed"] and observed.get(
                "guard_reason"
            ) not in {None, "TICKET_EXPIRED", "TICKET_ALREADY_CONSUMED", "TICKET_WORKER_RESTARTED"}
            return result

        monkeypatch.setattr(ReminderDispatcher, "dispatch_email", boundary)
        dispatch_maintained(client, store, human)
        for key, value in case["expected"].items():
            assert key in observed and observed[key] == value, (case["id"], key, observed)
        boundary_observer.assert_observed(case["id"], {key: observed[key] for key in case["expected"]})


@pytest.mark.parametrize("failure_point", ["business_commit", "provider_response"])
def test_unknown_delivery_never_blind_retries_and_vendor_ttl_does_not_release_key(
    monkeypatch: Any, failure_point: Any
) -> None:
    import asyncio
    from datetime import timedelta

    from reminder_support import dispatch_one, reminder_world
    from sqlalchemy import func, select

    from aco.features.actions.service import ActionService
    from aco.features.communications.models import EmailSubmission
    from aco.features.identity.models import now
    from aco.integrations.demo_email.models import NativeEmailInvocation, NativeEmailOperation
    from aco.worker.jobs import DomainExecutor, execute_action

    original = ActionService.complete_action

    def rollback_after_acceptance(service: Any, ctx: Any, lease: Any, result: Any) -> Any:
        if result.status.value == "succeeded":
            raise RuntimeError("Injected business commit failure after independent native acceptance")
        return original(service, ctx, lease, result)

    with reminder_world() as (client, store, ctx, invoices, saved):
        if failure_point == "business_commit":
            monkeypatch.setattr(ActionService, "complete_action", rollback_after_acceptance)
        else:
            from aco.core.types import ProviderErrorCode, ProviderFailure
            from aco.integrations.demo_email.adapter import DemoEmailAdapter
            from aco.integrations.ports.email import EmailFailure

            send = DemoEmailAdapter.send_email

            async def lose_response(adapter: Any, actor: Any, command: Any) -> Any:
                await send(adapter, actor, command)
                raise EmailFailure(
                    ProviderFailure(
                        code=ProviderErrorCode.UNKNOWN_SIDE_EFFECT, retryable=False, may_have_side_effect=True
                    )
                )

            monkeypatch.setattr(DemoEmailAdapter, "send_email", lose_response)
        action = dispatch_one(client, store, ctx)
        assert action.status == "unknown_outcome", action.last_error
        assert not action.automatic_resend
        submission = store.scalar(select(EmailSubmission))
        assert submission
        assert submission.state == ("accepted" if failure_point == "business_commit" else "unknown_outcome")
        listed = client.get(f"/api/workspaces/{ctx.tenant_id}/communications").json()
        assert listed["items"][0]["delivery_state"] == "unknown_outcome"
        native = store.scalar(select(NativeEmailOperation))
        assert native
        native.vendor_key_expires_at = now() - timedelta(seconds=1)
        action_id, version, job = action.id, action.version, action.job_id
        store.commit()

        async def repeat() -> Any:
            await execute_action(
                client.app.state.business_sessions, action_id, version, DomainExecutor(), job_id=job
            )

        asyncio.run(repeat())
        response = client.post(
            f"/api/workspaces/{ctx.tenant_id}/scheduled-actions/{action_id}/reconcile-delivery"
        )
        assert response.status_code == 200, response.text
        assert response.json()["state"] == (
            "accepted" if failure_point == "business_commit" else "unknown_outcome"
        )
        assert response.json()["resubmitted"] is False
        store.rollback()
        assert store.scalar(select(func.count()).select_from(NativeEmailInvocation)) == 1
        assert store.scalar(select(func.count()).select_from(NativeEmailOperation)) == 1


@pytest.mark.parametrize("changed", ["paid", "hold", "stale", "optout", "pause"])
def test_paid_hold_stale_optout_pause_block_dispatch(changed: Any, monkeypatch: Any) -> None:
    from uuid import uuid4

    from reminder_support import dispatch_one, reminder_world
    from sqlalchemy import func, select

    from aco.core.types import Money, ProviderErrorCode, ProviderFailure
    from aco.features.accounting.dto import DemoPaymentCommand
    from aco.features.accounting.models import InvoiceSnapshot
    from aco.features.accounting.seed import simulate_payment
    from aco.features.actions.models import ScheduledAction
    from aco.features.communications.outbound import ReminderDispatcher
    from aco.features.policies.dto import RecipientSave
    from aco.features.policies.service import PolicyService
    from aco.integrations.demo_accounting.adapter import DemoAccountingAdapter
    from aco.integrations.demo_email.models import NativeEmailInvocation
    from aco.integrations.ports.accounting import AccountingFailure

    original = ReminderDispatcher.archive
    with reminder_world() as (client, store, human, invoices, saved):

        async def changed_after_archive(dispatcher: Any, worker: Any, binding_id: Any, grant_id: Any) -> Any:
            await original(dispatcher, worker, binding_id, grant_id)
            async with dispatcher.sessions.begin() as db:
                action = await db.get(ScheduledAction, worker.action_id)
                invoice = await db.get(InvoiceSnapshot, action.invoice_id)
                if changed == "paid":
                    await simulate_payment(
                        db,
                        human,
                        DemoPaymentCommand(
                            provider_connection_id=action.provider_connection_id,
                            external_invoice_id=invoice.external_invoice_id,
                            amount=Money(
                                minor_units=invoice.balance_minor,
                                currency=invoice.currency,
                                exponent=invoice.exponent,
                            ),
                            operation_id=uuid4(),
                        ),
                        "local",
                    )
                elif changed == "hold":
                    await db.run_sync(
                        lambda session: PolicyService(session).place_hold(
                            human, invoice.id, "manual_hold", "Investigating after archive"
                        )
                    )
                elif changed == "pause":
                    await db.run_sync(
                        lambda session: PolicyService(session).set_outbound_pause(human, "tenant", True)
                    )
                elif changed == "optout":

                    def optout(session: Any) -> Any:
                        service = PolicyService(session)
                        contact = next(
                            c for c in service.recipients(human) if c.customer_id == invoice.customer_id
                        )
                        service.save_recipient(
                            human,
                            contact.contact_id,
                            RecipientSave(
                                expected_version=contact.version,
                                expected_contact_revision=contact.source_revision,
                                expected_destination=contact.destination,
                                timezone=contact.timezone,
                                timezone_verified=True,
                                owner_confirmed_tenant_timezone=False,
                                destination_verified=True,
                                allowed_channels=["email"],
                                opt_out=True,
                                evidence="Customer stop-contact instruction",
                            ),
                        )

                    await db.run_sync(optout)
            if changed == "stale":

                async def unavailable(*args: Any, **kwargs: Any) -> Any:
                    raise AccountingFailure(
                        ProviderFailure(
                            code=ProviderErrorCode.TEMPORARILY_UNAVAILABLE,
                            retryable=True,
                            may_have_side_effect=False,
                        )
                    )

                monkeypatch.setattr(DemoAccountingAdapter, "get_invoice", unavailable)

        monkeypatch.setattr(ReminderDispatcher, "archive", changed_after_archive)
        action = dispatch_one(client, store, human)
        assert action.status != "succeeded"
        assert store.scalar(select(func.count()).select_from(NativeEmailInvocation)) == 0
        print({"guard_change": changed, "status": action.status, "reason": action.last_error})


def test_two_invoices_keep_distinct_threads_under_customer_cap() -> None:
    from reminder_support import dispatch_one, reminder_world
    from sqlalchemy import func, select

    from aco.features.accounting.models import InvoiceSnapshot
    from aco.features.actions.models import ScheduledAction
    from aco.features.communications.models import CommunicationThread
    from aco.integrations.demo_email.models import NativeEmailInvocation

    with reminder_world() as (client, store, ctx, invoices, saved):
        first = dispatch_one(client, store, ctx)
        assert first.status == "succeeded"
        source = store.get(InvoiceSnapshot, first.invoice_id)
        other_invoice = store.scalar(
            select(InvoiceSnapshot)
            .join(ScheduledAction, ScheduledAction.invoice_id == InvoiceSnapshot.id)
            .where(
                InvoiceSnapshot.customer_id == source.customer_id,
                InvoiceSnapshot.id != source.id,
                ScheduledAction.status == "queued",
            )
        )
        assert other_invoice
        blocked = dispatch_one(client, store, ctx, other_invoice.id)
        assert blocked.status == "failed" and blocked.last_error in {
            "customer_contact_cap",
            "recipient_contact_cap",
        }
        assert blocked.operation_key != first.operation_key
        different = store.scalar(
            select(InvoiceSnapshot)
            .join(ScheduledAction, ScheduledAction.invoice_id == InvoiceSnapshot.id)
            .where(InvoiceSnapshot.customer_id != source.customer_id, ScheduledAction.status == "queued")
        )
        assert different
        sent = dispatch_one(client, store, ctx, different.id)
        assert sent.status == "succeeded", sent.last_error
        threads = list(store.scalars(select(CommunicationThread)))
        assert len(threads) == 2 and len({t.origin_invoice_id for t in threads}) == 2
        assert store.scalar(select(func.count()).select_from(NativeEmailInvocation)) == 2


def test_original_archive_retry_reuses_source_and_never_duplicates_email(monkeypatch: Any) -> None:
    from reminder_support import dispatch_one, reminder_world
    from sqlalchemy import func, select

    from aco.core.types import ProviderErrorCode, ProviderFailure
    from aco.features.artifacts.models import Artifact
    from aco.features.communications.models import Communication, CommunicationSource
    from aco.features.communications.outbound import ReminderDispatcher
    from aco.integrations.demo_email.models import NativeEmailInvocation
    from aco.integrations.ports.storage import StorageFailure

    original = ReminderDispatcher.archive
    interrupted: list[Any] = []

    async def interrupt_after_storage(dispatcher: Any, worker: Any, binding: Any, grant: Any) -> Any:
        await original(dispatcher, worker, binding, grant)
        if not interrupted:
            interrupted.append(True)
            raise StorageFailure(
                ProviderFailure(
                    code=ProviderErrorCode.TEMPORARILY_UNAVAILABLE, retryable=True, may_have_side_effect=False
                )
            )

    with reminder_world() as (client, store, ctx, invoices, saved):
        monkeypatch.setattr(ReminderDispatcher, "archive", interrupt_after_storage)
        first = dispatch_one(client, store, ctx)
        assert first.status == "queued", first.last_error
        first_id, invoice_id = first.id, first.invoice_id
        assert store.scalar(select(func.count()).select_from(NativeEmailInvocation)) == 0
        second = dispatch_one(client, store, ctx, invoice_id)
        assert second.id == first_id and second.status == "succeeded", second.last_error
        assert store.scalar(select(func.count()).select_from(NativeEmailInvocation)) == 1
        assert store.scalar(select(func.count()).select_from(Communication)) == 1
        assert store.scalar(select(func.count()).select_from(CommunicationSource)) == 1
        assert store.scalar(select(func.count()).select_from(Artifact)) == 1


def test_request_cap_counts_safe_retry_attempts(monkeypatch: Any) -> None:
    from reminder_support import dispatch_one, reminder_world
    from sqlalchemy import func, select

    from aco.core.types import ProviderErrorCode, ProviderFailure
    from aco.features.usage.models import UsageAttempt
    from aco.integrations.demo_email.adapter import DemoEmailAdapter
    from aco.integrations.ports.email import EmailFailure

    calls = []

    async def unavailable(adapter: Any, ctx: Any, command: Any) -> Any:
        calls.append(command.operation_id)
        raise EmailFailure(
            ProviderFailure(
                code=ProviderErrorCode.TEMPORARILY_UNAVAILABLE, retryable=True, may_have_side_effect=False
            )
        )

    with reminder_world(request_cap=1) as (client, store, ctx, invoices, saved):
        monkeypatch.setattr(DemoEmailAdapter, "send_email", unavailable)
        first = dispatch_one(client, store, ctx)
        assert first.status == "queued", first.last_error
        invoice_id = first.invoice_id
        second = dispatch_one(client, store, ctx, invoice_id)
        assert second.status == "failed" and second.last_error == "SYNTHETIC_REQUEST_CAP"
        assert len(calls) == 1
        assert (
            store.scalar(
                select(func.count()).select_from(UsageAttempt).where(UsageAttempt.operation == "send_email")
            )
            == 1
        )
