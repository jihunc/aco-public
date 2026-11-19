"""Inbound run producer bounds; fake fixtures are not claimed as verified ingress."""

from uuid import UUID, uuid4

import pytest
from case_promise_support import communication, invoices
from identity_support import actor_client
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.features.communications.ingress_dto import InboundAgentSource
from aco.integrations.ports.email_context import EmailIngressContext


def test_linked_scope_preserves_all_invoices_and_rejects_ambiguous_links() -> None:
    from aco.features.agent.ingress import bounded_linked_scope
    from aco.features.communications.models import CommunicationInvoiceLink

    with actor_client("owner") as (client, db, _user, tenant):
        rows = invoices(client, db, tenant)[:2]
        assert len(rows) == 2
        msg = communication(db, tenant, rows, "Synthetic linked invoice scope fixture")
        assert set(bounded_linked_scope(db, msg) or ()) == {row.id for row in rows}
        link = db.scalar(
            select(CommunicationInvoiceLink).where(CommunicationInvoiceLink.communication_id == msg.id)
        )
        assert link is not None
        db.add(
            CommunicationInvoiceLink(
                tenant_id=tenant,
                communication_id=msg.id,
                invoice_id=link.invoice_id,
                claim_or_context_key="ambiguous-fixture",
                provenance="synthetic_fixture",
                confidence="ambiguous",
                evidence_excerpt="",
                evidence_offset=0,
                classification="requires_review",
            )
        )
        db.flush()
        assert bounded_linked_scope(db, msg) is None


def test_unlinked_and_over_three_invoice_sources_require_review_without_subset() -> None:
    from aco.features.accounting.models import InvoiceSnapshot
    from aco.features.agent.ingress import bounded_linked_scope
    from aco.features.communications.models import Communication

    with actor_client("owner") as (client, db, _user, tenant):
        invoices(client, db, tenant)
        rows = list(db.scalars(select(InvoiceSnapshot).where(InvoiceSnapshot.tenant_id == tenant)))
        assert len(rows) >= 4
        msg = communication(db, tenant, rows[:4], "Synthetic over-scope fixture")
        assert bounded_linked_scope(db, msg) is None
        unlinked = Communication(
            tenant_id=tenant,
            provider_connection_id=msg.provider_connection_id,
            thread_id=msg.thread_id,
            origin_invoice_id=msg.origin_invoice_id,
            operation_id=uuid4(),
            direction="incoming",
            channel="email",
            kind="SYNTHETIC_FIXTURE",
            synthetic=True,
            content={"text_body": "Unlinked synthetic fixture"},
            event_at=msg.event_at,
            delivery_state="received",
            source_state="synthetic_fixture",
        )
        db.add(unlinked)
        db.flush()
        assert bounded_linked_scope(db, unlinked) is None


def test_inbound_references_cannot_promote_a_human_context_to_worker_authority() -> None:
    from typing import Any, cast

    import pytest
    from fastapi import HTTPException

    from aco.features.agent.ingress import produce_inbound_run

    with actor_client("owner") as (_client, db, user, tenant):
        from accounting_support import context

        with pytest.raises(HTTPException) as denied:
            produce_inbound_run(db, cast(Any, context(tenant, user.id)), cast(Any, object()))
        assert denied.value.status_code == 403


@pytest.mark.parametrize("revoke_before_execution", [False, True])
async def test_signed_original_produces_one_fake_run_and_real_guarded_task(
    revoke_before_execution: bool,
) -> None:
    import time

    import pytest
    from action_support import workers
    from fastapi import HTTPException
    from ingress_support import ingress_world

    from aco.features.actions.models import ActionExecution, ScheduledAction
    from aco.features.agent.ingress import produce_inbound_run
    from aco.features.agent.models import AgentRun, AgentStep
    from aco.features.communications.models import EmailIngressReceipt
    from aco.features.identity.models import AuditEvent
    from aco.features.review.models import CollectionTask

    with ingress_world() as lane:
        receipt_id = await lane.deliver(lane.message())

        def producer(db: Session, ctx: EmailIngressContext, source: InboundAgentSource) -> UUID | None:
            with pytest.raises(HTTPException) as denied:
                produce_inbound_run(db, ctx, source.model_copy(update={"invoice_ids": ()}))
            assert denied.value.status_code == 403
            key = produce_inbound_run(db, ctx, source)
            assert key is not None and produce_inbound_run(db, ctx, source) == key
            if revoke_before_execution:
                from aco.features.communications.models import EmailIngressActivation
                from aco.features.identity.models import now

                activation = db.get(EmailIngressActivation, source.activation_id)
                assert activation is not None
                activation.revoked_at = now()
            return key

        lane.world.store.rollback()
        await lane.work(producer)
        world = lane.world
        world.store.rollback()
        receipt = world.store.get(EmailIngressReceipt, receipt_id)
        assert receipt is not None and receipt.state == "processed", (
            None if receipt is None else receipt.error_code
        )
        assert receipt.agent_run_id is not None
        run_id, communication_id = receipt.agent_run_id, receipt.communication_id
        run = world.store.get(AgentRun, run_id)
        assert run is not None and run.authenticity == "FAKE"
        assert run.initiator_id == world.ctx.actor_id and run.worker_actor_id != run.initiator_id
        assert run.source_receipt_id == receipt_id and run.source_communication_id == communication_id
        audit = world.store.scalar(
            select(AuditEvent).where(
                AuditEvent.target_id == run_id, AuditEvent.action == "agent.inbound_queued"
            )
        )
        assert audit is not None and audit.actor_id == run.worker_actor_id
        with workers(world, crash="domain"):
            deadline = time.monotonic() + 30
            while True:
                world.store.rollback()
                run = world.store.get(AgentRun, run_id, populate_existing=True)
                assert run is not None
                if run.status not in {"queued", "running"}:
                    break
                assert time.monotonic() < deadline
                time.sleep(0.1)
        if revoke_before_execution:
            assert run.status == "failed" and run.error_code == "RUN_AUTHORITY_REVOKED"
            assert run.model_state == "not_started" and not run.provider_attempted
            assert world.store.scalar(select(AgentStep).where(AgentStep.run_id == run_id)) is None
            assert (
                world.store.scalar(select(ScheduledAction).where(ScheduledAction.agent_run_id == run_id))
                is None
            )
            return
        assert run.status == "completed", run.error_code
        assert run.provider_attempted is False and run.authenticity == "FAKE"
        steps = list(world.store.scalars(select(AgentStep).where(AgentStep.run_id == run_id)))
        source_step = next(s for s in steps if s.tool_name == "get_communication_history")
        assert source_step.output is not None and str(communication_id) in str(source_step.output)
        action = world.store.scalar(select(ScheduledAction).where(ScheduledAction.agent_run_id == run_id))
        assert action is not None and action.source_event_id is None
        task = world.store.scalar(select(CollectionTask).where(CollectionTask.source_action_id == action.id))
        execution = world.store.scalar(select(ActionExecution).where(ActionExecution.action_id == action.id))
        assert task is not None and execution is not None and execution.status == "succeeded"
        assert world.submission_count(action.id) == 0
