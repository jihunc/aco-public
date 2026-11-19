"""Public fixed synthetic source -> private original -> maintained FAKE -> guarded hold."""

import asyncio
import json
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from ingress_support import IngressWorld, ingress_world
from pydantic import SecretStr
from sqlalchemy import func, select

from aco.core.settings import Settings
from aco.features.accounting.models import InvoiceSnapshot
from aco.features.agent.graph import dispatch_agent_runs
from aco.features.agent.ingress import produce_inbound_run
from aco.features.agent.models import AgentRun
from aco.features.agent.schemas import InvestigationResult
from aco.features.communications.ingress_api import router
from aco.features.communications.models import Communication, CommunicationSource, EmailIngressReceipt
from aco.features.policies.models import CollectionHold, RecipientControl
from aco.features.review.models import CollectionTask
from aco.worker.app import WorkerRuntime


async def finish(lane: IngressWorld) -> None:
    lane.world.store.rollback()
    await lane.work(produce_inbound_run)
    runtime = WorkerRuntime(Settings(database_url=SecretStr(lane.world.url)))
    try:
        async with runtime.app.open_async():
            await dispatch_agent_runs(runtime.sessions, runtime.app)
            await asyncio.wait_for(
                runtime.app.run_worker_async(queues=["aco"], concurrency=1, wait=False, listen_notify=False),
                timeout=40,
            )
    finally:
        await runtime.engine.dispose()


async def test_public_fixed_contact_stop_original_fake_action_and_release() -> None:
    with ingress_world() as lane:
        app = lane.world.client.app
        assert isinstance(app, FastAPI)
        app.include_router(router)
        app.state.email_ingress = lane.service
        root = f"/api/workspaces/{lane.world.ctx.tenant_id}"
        command = dict(
            request_id=str(uuid4()),
            expected_activation_version=1,
            invoice_ids=[str(lane.world.invoice_id)],
            scenario="contact_stop_reply",
        )
        path = root + f"/email-ingress/activations/{lane.activation_id}/simulate"
        response = lane.world.client.post(path, json=command)
        assert response.status_code == 202, response.text
        receipt_id = UUID(response.json()["id"])
        assert lane.world.client.post(path, json=command).json()["id"] == str(receipt_id)
        await finish(lane)
        lane.world.store.rollback()
        db = lane.world.store
        receipt = db.get(EmailIngressReceipt, receipt_id)
        assert receipt is not None and receipt.state == "processed" and receipt.agent_run_id
        source = db.get(Communication, receipt.communication_id)
        assert source is not None and source.source_state == "stored" and source.synthetic
        source_body = source.content["text_body"]
        assert isinstance(source_body, str)
        assert "SYNTHETIC DEMO SOURCE" in source_body
        assert "Please stop all contact" in source_body
        assert db.scalar(select(func.count()).select_from(CommunicationSource)) == 1
        run = db.get(AgentRun, receipt.agent_run_id)
        assert run is not None and run.status == "completed" and run.authenticity == "FAKE"
        assert not run.provider_attempted and run.source_communication_id == source.id
        assert run.result is not None
        proposal = InvestigationResult.model_validate_json(json.dumps(run.result)).proposals[0]
        assert proposal.kind == "request_collection_hold" and proposal.executed
        holds = list(db.scalars(select(CollectionHold).where(CollectionHold.released_at.is_(None))))
        assert {str(h.id) for h in holds} == set(map(str, proposal.changed_ids)) and len(holds) == 4
        assert db.scalar(select(func.count()).select_from(CollectionTask)) == 1
        assert db.scalar(select(func.count()).select_from(RecipientControl)) == 0
        invoice = db.get(InvoiceSnapshot, lane.world.invoice_id)
        assert invoice is not None and invoice.balance_minor == 10000 and invoice.financial_status == "open"
        review = lane.world.client.get(root + f"/invoices/{invoice.id}/contact-stops").json()["items"][0]
        released = lane.world.client.post(
            root + f"/contact-stops/{review['action_id']}/release",
            json=dict(
                request_id=str(uuid4()),
                expected_action_version=review["action_version"],
                reviewed_holds=[
                    dict(hold_id=h["id"], expected_version=h["version"]) for h in review["original_holds"]
                ],
                reason="Current Owner reviewed genuine stored fixed synthetic source",
            ),
        )
        assert released.status_code == 200 and not released.json()["pending"], released.text


@pytest.mark.parametrize("change", ["extra_text", "different_statement"])
async def test_fake_contact_stop_recognizes_only_exact_stored_demo_fixture(change: str) -> None:
    with ingress_world() as lane:
        invoice = lane.world.store.get(InvoiceSnapshot, lane.world.invoice_id)
        assert invoice is not None
        body = (
            "SYNTHETIC DEMO SOURCE. This is a fixed application test scenario, not a customer statement. "
            f"Invoice references: {invoice.number}. For this synthetic example: Please stop all contact "
            "for this customer until a human reviews this request."
        )
        body = (
            body + " Additional unsupported statement."
            if change == "extra_text"
            else body.replace("Please stop all contact", "Please review contact")
        )
        message = lane.message(body=body)
        lane.world.store.rollback()
        receipt_id = await lane.deliver(message)
        await finish(lane)
        lane.world.store.rollback()
        db = lane.world.store
        receipt = db.get(EmailIngressReceipt, receipt_id)
        assert receipt is not None and receipt.agent_run_id is not None
        run = db.get(AgentRun, receipt.agent_run_id)
        assert run is not None and run.status == "completed" and run.result is not None
        result = InvestigationResult.model_validate_json(json.dumps(run.result))
        assert all(p.kind != "request_collection_hold" for p in result.proposals)
        assert db.scalar(select(func.count()).select_from(CollectionHold)) == 0
        assert db.scalar(select(func.count()).select_from(CollectionTask)) == 1
        assert not run.provider_attempted and run.authenticity == "FAKE"
