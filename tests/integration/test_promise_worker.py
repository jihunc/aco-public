"""Canonical maintained promise cutoff work, independent of outreach enablement."""

import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from accounting_support import context
from case_promise_support import communication, invoices
from fastapi import FastAPI, HTTPException
from identity_support import actor_client
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from aco.core.context import ServiceContext
from aco.core.types import ActionResult, Money
from aco.features.accounting.models import AccountingObservation, InvoiceSnapshot
from aco.features.actions.models import ActionExecution, ScheduledAction
from aco.features.cases.dto import CommunicationEvidence
from aco.features.communications.models import Communication
from aco.features.identity.models import Membership, Tenant, now
from aco.features.identity.service import IdentityService, Permission
from aco.features.promises.dto import ConfirmPromise, RecordPromise
from aco.features.promises.service import PromiseService
from aco.worker.app import WorkerRuntime
from aco.worker.jobs import DomainExecutor


class CheckingPromiseExecutor(DomainExecutor):
    async def execute(self, ctx: ServiceContext, action: ScheduledAction, db: AsyncSession) -> ActionResult:
        if action.kind == "promise_check":
            await db.run_sync(lambda store: self.check(store, ctx))
        return await super().execute(ctx, action, db)

    def check(self, db: Session, ctx: ServiceContext) -> None:
        identity = IdentityService(db)
        identity.authorize(ctx, Permission.INVOICE_READ)
        from aco.features.promises.models import PromiseVerification
        from aco.features.promises.performance import record_promise_source_failure
        from aco.integrations.ports.accounting import AccountingRejectionEvidence, RejectedPaymentReference

        action = db.get(ScheduledAction, ctx.action_id)
        assert action is not None
        invoice = db.get(InvoiceSnapshot, action.invoice_id)
        assert invoice is not None
        negative_fixture = AccountingRejectionEvidence(
            tenant_id=ctx.tenant_id,
            provider_connection_id=action.provider_connection_id,
            external_invoice_id=invoice.external_invoice_id,
            reason="payment_parent_unresolved",
            candidate=RejectedPaymentReference(native_source_id="NEGATIVE-ONLY", kind="reversal"),
        )
        before = db.scalar(select(func.count()).select_from(PromiseVerification))
        for forged in (
            {"tenant_id": uuid4()},
            {"provider_connection_id": uuid4()},
            {"external_invoice_id": "OTHER-INVOICE"},
        ):
            with (
                pytest.raises(HTTPException, match="PROMISE_REJECTED_SOURCE_OUTSIDE_ACTION"),
                db.begin_nested(),
            ):
                record_promise_source_failure(
                    db,
                    ctx,
                    invoice.id,
                    "VALIDATION_FAILED",
                    rejection_evidence=negative_fixture.model_copy(update=forged),
                )
        assert db.scalar(select(func.count()).select_from(PromiseVerification)) == before
        with pytest.raises(HTTPException, match="PROMISE_EXECUTION_NOT_CURRENT"), db.begin_nested():
            attempt = db.scalar(select(ActionExecution).where(ActionExecution.lease_token == ctx.lease_token))
            assert attempt is not None
            attempt.attempt += 1
            db.flush()
            identity.authorize(ctx, Permission.INVOICE_READ)
        with pytest.raises(HTTPException, match="PROMISE_EXECUTION_NOT_CURRENT"), db.begin_nested():
            db.execute(
                text(
                    "UPDATE procrastinate_workers SET last_heartbeat=now()-interval '5 minutes' "
                    "WHERE id=(SELECT worker_id FROM procrastinate_jobs WHERE id=:id)"
                ),
                {"id": ctx.job_id},
            )
            identity.authorize(ctx, Permission.INVOICE_READ)


@pytest.mark.parametrize(
    ("revoked", "source_unavailable", "rejection_kind"),
    [
        (False, False, None),
        (True, False, None),
        (False, True, None),
        (False, False, "parentless"),
        (False, False, "currency"),
    ],
)
def test_actual_promise_cutoff_checks_source_without_outreach_authority(
    revoked: bool, source_unavailable: bool, rejection_kind: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    with actor_client("owner") as (client, db, user, tenant):
        invoice = invoices(client, db, tenant)[0]
        invoice_id = invoice.id
        msg = communication(
            db, tenant, [invoice], "Synthetic documentary review: unpaid AUD40 was due yesterday."
        )
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
                due_date=(now() - timedelta(days=1)).date(),
                timezone="UTC",
            ),
        )
        confirmed = service.confirm_promise(
            ctx,
            ConfirmPromise(
                promise_id=captured.id,
                expected_version=captured.version,
                confirmation_evidence=evidence,
                reason="Owner documentary confirmation of this overdue synthetic commitment.",
            ),
        )
        promise_id = confirmed.id
        db.commit()
        action = db.scalar(select(ScheduledAction).where(ScheduledAction.kind == "promise_check"))
        assert action is not None
        action_id = action.id
        before = db.scalar(select(func.count()).select_from(AccountingObservation))
        assert before is not None
        member = db.scalar(
            select(Membership).where(Membership.tenant_id == tenant, Membership.user_id == user.id)
        )
        workspace = db.get(Tenant, tenant)
        assert member is not None and workspace is not None
        if revoked:
            member.active = False
            member.version += 1
        else:
            # An unrelated outbound configuration generation cannot cancel source verification.
            workspace.version += 1
        db.commit()
        if rejection_kind is not None:
            from aco.features.accounting.models import DemoSourceRecord

            db.add(
                DemoSourceRecord(
                    tenant_id=tenant,
                    provider_connection_id=invoice.provider_connection_id,
                    organisation_id=invoice.organisation_id,
                    kind="payment",
                    external_id="actual-rejected-R1",
                    revision=1,
                    effective_at=now(),
                    observed_at=now(),
                    payload={
                        "external_payment_id": "R1",
                        "external_allocation_id": "R1-ALLOCATION",
                        "external_invoice_id": invoice.external_invoice_id,
                        "kind": "reversal",
                        "amount": {
                            "minor_units": -100,
                            "currency": "USD" if rejection_kind == "currency" else "AUD",
                            "exponent": 2,
                        },
                        "parent_payment_id": None if rejection_kind == "parentless" else "P1",
                        "parent_allocation_id": None if rejection_kind == "parentless" else "P1-ALLOCATION",
                    },
                )
            )
            db.commit()
        if source_unavailable:
            from aco.core.context import ActorContext
            from aco.core.types import ProviderErrorCode
            from aco.features.accounting.dto import AccountingResult, InvoiceData
            from aco.integrations.demo_accounting.adapter import DemoAccountingAdapter
            from aco.integrations.ports.accounting import failure

            async def unavailable(
                adapter: DemoAccountingAdapter, actor: ActorContext, invoice_key: str
            ) -> AccountingResult[InvoiceData]:
                raise failure(ProviderErrorCode.TEMPORARILY_UNAVAILABLE)

            monkeypatch.setattr(DemoAccountingAdapter, "get_invoice", unavailable)

        async def work() -> None:
            application = client.app
            assert isinstance(application, FastAPI)
            runtime = WorkerRuntime(application.state.settings, executor=CheckingPromiseExecutor())
            try:
                await runtime.app.open_async()
                await runtime.dispatcher.dispatch_outbox(100)
                await runtime.app.run_worker_async(
                    queues=["aco"], concurrency=1, wait=False, install_signal_handlers=False
                )
            finally:
                await runtime.app.close_async()
                await runtime.engine.dispose()

        asyncio.run(work())
        db.expire_all()
        completed = db.get(ScheduledAction, action_id)
        assert completed is not None
        if revoked:
            assert completed.status == "failed" and completed.last_error in {
                "INITIATOR_REVOKED",
                "PROMISE_CONFIRMING_AUTHORITY_REVOKED",
            }
            assert db.scalar(select(func.count()).select_from(AccountingObservation)) == before
        elif source_unavailable or rejection_kind is not None:
            assert completed.status == ("queued" if source_unavailable else "failed")
            assert completed.last_error == (
                "TEMPORARILY_UNAVAILABLE" if source_unavailable else "VALIDATION_FAILED"
            )
            view = service.get(ctx, promise_id)
            assert view.status == "ACTIVE" and view.verification_status == "PENDING_REVIEW"
            assert db.scalar(select(func.count()).select_from(AccountingObservation)) == before
            from aco.features.accounting.models import CollectionRecord

            collection = db.scalar(select(CollectionRecord).where(CollectionRecord.invoice_id == invoice_id))
            assert collection is not None and "promise_verification" in collection.hold_reasons
            from aco.features.accounting.models import VerifiedPaymentAllocation
            from aco.features.promises.models import PromiseVerification

            verification = db.scalar(
                select(PromiseVerification).where(PromiseVerification.promise_id == promise_id)
            )
            assert verification is not None
            failed_source = verification.details["source_failure"]
            assert isinstance(failed_source, dict)
            rejected = failed_source.get("rejection_evidence")
            if rejection_kind is None:
                assert rejected is None
            else:
                assert isinstance(rejected, dict) and rejected["authenticity"] == "UNVERIFIED"
                assert rejected["provider_connection_id"] == str(completed.provider_connection_id)
                candidate = rejected["candidate"]
                assert isinstance(candidate, dict) and candidate["external_payment_id"] == "R1"
                assert candidate["kind"] == "reversal" and "amount" not in candidate
                assert not list(
                    db.scalars(
                        select(VerifiedPaymentAllocation).where(
                            VerifiedPaymentAllocation.external_payment_id == "R1"
                        )
                    )
                )
        else:
            assert completed.status == "succeeded", completed.last_error
            view = service.get(ctx, promise_id)
            assert view.status == "MISSED" and view.verification_status == "VERIFIED"
            assert db.scalar(select(func.count()).select_from(AccountingObservation)) == before + 1
            observation = db.scalar(
                select(AccountingObservation).where(
                    AccountingObservation.actor_id == completed.worker_actor_id,
                    AccountingObservation.invoice_id == invoice_id,
                )
            )
            assert observation is not None and observation.execution_binding is not None
            assert UUID(str(observation.execution_binding["action_id"])) == action_id
        current = db.get(InvoiceSnapshot, invoice_id)
        assert current is not None and current.balance_minor == 10000 and current.financial_status == "open"
        assert not list(db.scalars(select(Communication).where(Communication.direction == "outgoing")))
        assert not list(db.scalars(select(ScheduledAction).where(ScheduledAction.kind == "policy_due")))


def test_verified_miss_reconsiders_only_current_independently_enabled_policy() -> None:
    from integration.test_policy_authority import setup_active
    from policy_support import policy_world

    with policy_world() as (client, db, ctx, current_invoices, saved):
        invoice = next(i for i in current_invoices if i.number == "INV-101")
        invoice_id = invoice.id
        msg = communication(db, ctx.tenant_id, [invoice], "Documentary AUD40 commitment due yesterday.")
        evidence = CommunicationEvidence(
            communication_id=msg.id, excerpt=str(msg.content["text_body"]), offset=0
        )
        service = PromiseService(db)
        captured = service.record_promise(
            ctx,
            RecordPromise(
                customer_id=invoice.customer_id,
                invoice_ids=[invoice_id],
                evidence=evidence,
                amount=Money(minor_units=4000, currency="AUD", exponent=2),
                due_date=(now() - timedelta(days=1)).date(),
                timezone="UTC",
            ),
        )
        confirmed = service.confirm_promise(
            ctx,
            ConfirmPromise(
                promise_id=captured.id,
                expected_version=captured.version,
                confirmation_evidence=evidence,
                reason="Owner documentary confirmation of overdue synthetic terms.",
            ),
        )
        promise_id = confirmed.id
        db.commit()
        setup_active(client, db, ctx, saved)
        assert not list(
            db.scalars(
                select(ScheduledAction).where(
                    ScheduledAction.invoice_id == invoice_id,
                    ScheduledAction.kind == "policy_due",
                    ScheduledAction.status == "queued",
                )
            )
        )
        db.commit()

        async def work() -> None:
            application = client.app
            assert isinstance(application, FastAPI)
            runtime = WorkerRuntime(application.state.settings, executor=CheckingPromiseExecutor())
            try:
                await runtime.app.open_async()
                await runtime.dispatcher.dispatch_outbox(100)
                await runtime.app.run_worker_async(
                    queues=["aco"], concurrency=1, wait=False, install_signal_handlers=False
                )
            finally:
                await runtime.app.close_async()
                await runtime.engine.dispose()

        asyncio.run(work())
        db.expire_all()
        assert service.get(ctx, promise_id).status == "MISSED"
        future = list(
            db.scalars(
                select(ScheduledAction).where(
                    ScheduledAction.invoice_id == invoice_id,
                    ScheduledAction.kind == "policy_due",
                    ScheduledAction.status == "queued",
                )
            )
        )
        assert len(future) == 1
        assert future[0].due_at > now() and future[0].policy_activation_id is not None
        assert future[0].promise_event_id is None and future[0].initiator_id == ctx.actor_id
        assert not list(db.scalars(select(Communication).where(Communication.direction == "outgoing")))
