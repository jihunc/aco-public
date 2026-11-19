"""Exact public three-intent synthetic original; genuine scoped reads and domain outcomes."""

import json
from uuid import UUID, uuid4

from fastapi import FastAPI
from ingress_support import ingress_world
from integration.test_contact_stop_ingress import finish
from sqlalchemy import func, select

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.agent.models import AgentRun, AgentStep
from aco.features.agent.schemas import InvestigationResult
from aco.features.cases.models import CaseEvidence, CollectionCase
from aco.features.communications.ingress_api import router
from aco.features.communications.models import Communication, CommunicationSource, EmailIngressReceipt
from aco.features.identity.models import AuditEvent
from aco.features.policies.models import CollectionHold
from aco.features.promises.models import PaymentPromise
from aco.features.review.models import CollectionTask


async def test_fixed_three_intent_source_produces_reviewable_promise_case_and_payment_task() -> None:
    with ingress_world() as lane:
        app = lane.world.client.app
        assert isinstance(app, FastAPI)
        app.include_router(router)
        app.state.email_ingress = lane.service
        root = f"/api/workspaces/{lane.world.ctx.tenant_id}"
        rows = list(
            lane.world.store.scalars(
                select(InvoiceSnapshot)
                .where(
                    InvoiceSnapshot.tenant_id == lane.world.ctx.tenant_id,
                    InvoiceSnapshot.number.in_(["INV-101", "INV-107", "INV-113"]),
                )
                .order_by(InvoiceSnapshot.number)
            )
        )
        assert len(rows) == 3 and len({r.customer_id for r in rows}) == 1
        before = [(r.id, r.balance_minor, r.financial_status, r.source_revision) for r in rows]
        command = dict(
            request_id=str(uuid4()),
            expected_activation_version=1,
            invoice_ids=[str(r.id) for r in rows],
            scenario="three_intent_reply",
        )
        path = root + f"/email-ingress/activations/{lane.activation_id}/simulate"
        rejected = lane.world.client.post(
            path, json=dict(command, request_id=str(uuid4()), invoice_ids=[str(rows[0].id)])
        )
        assert rejected.status_code == 422
        response = lane.world.client.post(path, json=command)
        assert response.status_code == 202, response.text
        receipt_id = UUID(response.json()["id"])
        assert lane.world.client.post(path, json=command).json()["id"] == str(receipt_id)
        await finish(lane)
        lane.world.store.rollback()
        db = lane.world.store
        receipt = db.get(EmailIngressReceipt, receipt_id)
        assert receipt and receipt.communication_id and receipt.agent_run_id
        source = db.get(Communication, receipt.communication_id)
        assert source and source.synthetic and source.source_state == "stored"
        assert db.scalar(select(CommunicationSource).where(CommunicationSource.communication_id == source.id))
        run = db.get(AgentRun, receipt.agent_run_id)
        assert run and run.authenticity == "FAKE" and not run.provider_attempted
        assert run.status == "completed", (run.error_code, run.result)
        assert run.result is not None
        result = InvestigationResult.model_validate_json(json.dumps(run.result))
        assert [p.kind for p in result.proposals] == ["record_promise", "upsert_case", "create_task"]
        assert all(p.executed for p in result.proposals)
        saved_promise = db.get(PaymentPromise, result.proposals[0].changed_ids[0])
        case = db.get(CollectionCase, result.proposals[1].changed_ids[0])
        task = db.get(CollectionTask, result.proposals[2].changed_ids[0])
        assert saved_promise and saved_promise.status == "NEEDS_CONFIRMATION"
        assert (
            saved_promise.amount == {"minor_units": 10000, "currency": "AUD", "exponent": 2}
            and saved_promise.source_communication_id == source.id
        )
        assert case and case.issue_type == "wrong_amount" and case.status == "OPEN"
        assert task and task.invoice_id == rows[2].id and "payment claim" in task.title.lower()
        reads = list(
            db.scalars(select(AgentStep).where(AgentStep.run_id == run.id, AgentStep.kind == "read"))
        )
        assert len(reads) <= 8 and all(s.status == "completed" for s in reads)
        assert any(s.invoice_id == rows[2].id and s.tool_name == "get_verified_payments" for s in reads)
        active = list(db.scalars(select(CollectionHold).where(CollectionHold.released_at.is_(None))))
        assert any(h.invoice_id == rows[1].id and h.reason == "active_dispute" for h in active)
        assert not any(h.invoice_id == rows[2].id for h in active)
        db.expire_all()
        assert [(r.id, r.balance_minor, r.financial_status, r.source_revision) for r in rows] == before

        history = next(s for s in reads if s.tool_name == "get_communication_history")
        reuse = list(
            db.scalars(select(AuditEvent).where(AuditEvent.action == "agent.original_source_reused"))
        )
        assert all(a.details["read_step_id"] == str(history.id) for a in reuse)
        assert any(s.tool_name == "find_existing_cases" and s.invoice_id == rows[1].id for s in reads)
        assert len([s for s in reads if s.tool_name == "get_communication_history"]) == 1
        assert len(reads) == 8
        promise_id, case_id = saved_promise.id, case.id
        source_id, source_body = source.id, source.content["text_body"]
        confirmed = lane.world.client.post(
            root + f"/promises/{promise_id}/confirm",
            json=dict(
                promise_id=str(promise_id),
                expected_version=saved_promise.version,
                confirmation_evidence=dict(communication_id=str(source_id), offset=0, excerpt=source_body),
                reason="Owner independently reviewed this exact stored synthetic commitment",
            ),
        )
        assert confirmed.status_code == 200 and confirmed.json()["status"] == "ACTIVE", confirmed.text
        # A later genuine original with the same issue must be read and append evidence to this case.
        repeat = lane.world.client.post(path, json=dict(command, request_id=str(uuid4())))
        assert repeat.status_code == 202, repeat.text
        repeat_id = UUID(repeat.json()["id"])
        await finish(lane)
        db.rollback()
        later = db.get(EmailIngressReceipt, repeat_id)
        assert later and later.communication_id != source_id and later.agent_run_id
        later_run = db.get(AgentRun, later.agent_run_id)
        assert later_run and later_run.status == "completed" and later_run.result, (
            later_run.error_code if later_run else None
        )
        later_result = InvestigationResult.model_validate_json(json.dumps(later_run.result))
        assert later_result.proposals[1].changed_ids == [case_id] and later_result.proposals[1].executed
        assert db.scalar(select(func.count()).select_from(CollectionCase)) == 1
        assert (
            db.scalar(select(func.count()).select_from(CaseEvidence).where(CaseEvidence.case_id == case_id))
            == 2
        )
        retained = db.get(PaymentPromise, promise_id)
        assert retained and retained.status == "ACTIVE"
        db.expire_all()
        assert [(r.id, r.balance_minor, r.financial_status, r.source_revision) for r in rows] == before
