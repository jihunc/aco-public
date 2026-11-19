"""Maintained worker proof for exact observation-bound post-read continuation."""

import asyncio
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from accounting_support import publish_invoice
from fastapi import HTTPException
from integration.test_policy_authority import setup_active
from policy_support import policy_world
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from aco.core.context import ServiceContext
from aco.core.types import ActionResult
from aco.features.accounting.models import AccountingEvent, AccountingObservation, InvoiceSnapshot
from aco.features.accounting.reconcile import SourceReadContext
from aco.features.actions.models import ScheduledAction
from aco.features.actions.service import ActionService, ScheduledCommand
from aco.features.identity.models import now
from aco.features.identity.service import IdentityService, Permission
from aco.features.policies.dto import OutboundProposal
from aco.features.policies.guards import PolicyGuards
from aco.worker.app import WorkerRuntime
from aco.worker.jobs import DomainExecutor


class CheckingExecutor(DomainExecutor):
    checked = False

    async def execute(self, ctx: ServiceContext, action: ScheduledAction, db: AsyncSession) -> ActionResult:
        try:
            result = await super().execute(ctx, action, db)
            await db.run_sync(lambda store: self.check(store, ctx, action))
        except Exception:
            import traceback

            traceback.print_exc()
            raise
        self.checked = True
        return result

    def check(self, db: Session, ctx: ServiceContext, action: ScheduledAction) -> None:
        observation = db.scalar(
            select(AccountingObservation)
            .where(
                AccountingObservation.invoice_id == action.invoice_id,
                AccountingObservation.actor_id == ctx.actor_id,
            )
            .order_by(AccountingObservation.created_at.desc())
        )
        assert observation is not None
        proof = SourceReadContext(**ctx.model_dump(), observation_id=observation.id)
        identity = IdentityService(db)
        assert identity.authorize(proof, Permission.INVOICE_READ).id == ctx.tenant_id
        with pytest.raises(DBAPIError, match="immutable"), db.begin_nested():
            db.execute(
                update(AccountingObservation)
                .where(AccountingObservation.id == observation.id)
                .values(execution_binding={})
            )
        with pytest.raises(HTTPException, match="SOURCE_CONTINUATION_ACTION_MUTATION_DENIED"):
            ActionService(db).locked(proof, action.id)
        with pytest.raises(HTTPException, match="STALE_ACTION_SCOPE"):
            identity.authorize(ctx, Permission.INVOICE_READ)
        for permission in (Permission.DEMO_PAYMENT, Permission.TASK_WRITE, Permission.APPROVE_EXTERNAL):
            with pytest.raises(HTTPException, match="SOURCE_CONTINUATION_PERMISSION_DENIED"):
                identity.authorize(proof, permission)
        for forged in ({"observation_id": uuid4()}, {"tenant_id": uuid4()}, {"lease_token": uuid4()}):
            with pytest.raises(HTTPException):
                identity.authorize(proof.model_copy(update=forged), Permission.INVOICE_READ)
        invoice = db.get(InvoiceSnapshot, action.invoice_id)
        assert invoice is not None
        other = db.scalar(
            select(InvoiceSnapshot).where(
                InvoiceSnapshot.tenant_id == ctx.tenant_id, InvoiceSnapshot.id != invoice.id
            )
        )
        assert other is not None
        from aco.features.policies.service import PolicyService

        with pytest.raises(HTTPException, match="SCHEDULING_SCOPE_OUTSIDE_ACTION"):
            PolicyService(db).recompute_next_action(proof, other.id, now())
        # Actual original observation is immutable. These deliberately malformed new rows
        # are negative-only fixtures; no positive grant or execution is fabricated.
        for mutation in (
            "foreign_invoice",
            "foreign_actor",
            "old_attempt",
            "stale",
            "unverified",
            "incarnation",
        ):
            data = deepcopy(observation.invoice_data)
            provenance = deepcopy(observation.provenance)
            execution = deepcopy(observation.execution_binding)
            assert execution is not None
            if mutation == "old_attempt":
                execution["attempt_id"] = str(uuid4())
            if mutation == "incarnation":
                execution["incarnation"] = str(uuid4())
            if mutation == "unverified":
                data["verified"] = False
            bad = AccountingObservation(
                tenant_id=ctx.tenant_id,
                provider_connection_id=observation.provider_connection_id,
                actor_id=action.initiator_id if mutation == "foreign_actor" else ctx.actor_id,
                invoice_id=other.id if mutation == "foreign_invoice" else invoice.id,
                invoice_data=data,
                payment_data=observation.payment_data,
                provenance=provenance,
                execution_binding=execution,
                observed_at=now() - timedelta(hours=1) if mutation == "stale" else observation.observed_at,
            )
            with db.begin_nested() as savepoint:
                db.add(bad)
                db.flush()
                with pytest.raises(HTTPException, match="INVALID_SOURCE_CONTINUATION"):
                    identity.authorize(
                        proof.model_copy(update={"observation_id": bad.id}), Permission.INVOICE_READ
                    )
                savepoint.rollback()
        until = action.lease_until
        action.lease_until = now() - timedelta(seconds=1)
        db.flush()
        with pytest.raises(HTTPException, match="STALE_SERVICE_AUTHORITY"):
            identity.authorize(proof, Permission.INVOICE_READ)
        action.lease_until = until
        db.flush()
        proposal = OutboundProposal(
            action_id=action.id,
            attempt_id=uuid4(),
            read_id=uuid4(),
            channel="email",
            destination="unreviewed@example.test",
        )
        guards = PolicyGuards(db, now())
        for attempt in (
            lambda: guards.evaluate_outbound(proof, proposal, now()),
            lambda: guards.reserve_contact(proof, invoice.id, "unreviewed@example.test", "email", uuid4()),
            lambda: guards.consume_dispatch(proof, uuid4(), proposal),
        ):
            with pytest.raises(HTTPException, match="SOURCE_CONTINUATION_OUTREACH_DENIED"):
                attempt()
        assert identity.authorize(proof, Permission.INVOICE_READ).id == ctx.tenant_id


def test_worker_reconciles_new_revision_without_promoting_original_authority() -> None:
    with policy_world() as (client, store, ctx, invoices, saved):
        saved = setup_active(client, store, ctx, saved)
        invoice = next(i for i in invoices if i.number == "INV-101")
        invoice_id = invoice.id
        old = store.scalar(
            select(ScheduledAction).where(
                ScheduledAction.invoice_id == invoice_id, ScheduledAction.status == "queued"
            )
        )
        source = store.scalar(select(AccountingEvent).where(AccountingEvent.invoice_id == invoice_id))
        assert old and source
        assert old.policy_version_id is not None
        old_id = old.id
        ref = ActionService(store).enqueue_action(
            ctx,
            ScheduledCommand(
                kind="accounting_recheck",
                provider_connection_id=invoice.provider_connection_id,
                invoice_id=invoice_id,
                policy_version_id=old.policy_version_id,
                source_event_id=source.id,
                due_at=now(),
            ),
        )
        store.commit()
        publish_invoice(
            store,
            ctx.tenant_id,
            invoice.provider_connection_id,
            invoice.external_invoice_id,
            source_status="CORRECTED_IN_SOURCE",
        )
        store.rollback()
        executor = CheckingExecutor()

        async def drain() -> None:
            runtime = WorkerRuntime(client.app.state.settings, executor)
            try:
                await runtime.app.open_async()
                await runtime.dispatcher.dispatch_outbox(100)
                await runtime.app.run_worker_async(
                    queues=["aco"], concurrency=1, wait=False, install_signal_handlers=False
                )
            finally:
                await runtime.app.close_async()
                await runtime.engine.dispose()

        asyncio.run(drain())
        store.expire_all()
        action = store.get(ScheduledAction, ref.action_id)
        assert action and action.status == "succeeded" and executor.checked
        old = store.get(ScheduledAction, old_id)
        assert old and old.status == "cancelled"
        pending = list(
            store.scalars(
                select(ScheduledAction).where(
                    ScheduledAction.invoice_id == invoice_id,
                    ScheduledAction.kind == "policy_due",
                    ScheduledAction.status == "queued",
                )
            )
        )
        assert len(pending) == 1 and pending[0].source_revision == "2" and pending[0].due_at > now()
