"""Fixed synthetic application scenarios; no arbitrary source or real customer capture."""

import asyncio
from typing import cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from ingress_support import ingress_world
from sqlalchemy import func, select

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.communications.ingress_api import router
from aco.features.communications.models import Communication, CommunicationSource, EmailIngressReceipt
from aco.integrations.demo_email.models import NativeInboundMessage


@pytest.mark.parametrize(
    "scenario",
    [
        "invoice_reply",
        "promise_reply",
        "ambiguous_promise_reply",
        "case_reply",
        "case_resolution",
        "multi_promise_reply",
        "multi_promise_amendment",
        "installment_promise_reply",
    ],
)
def test_owner_simulation_uses_persisted_original_on_exact_replay_and_rejects_changes(scenario: str) -> None:
    with ingress_world() as lane:
        app = cast(FastAPI, lane.world.client.app)
        app.include_router(router)
        app.state.email_ingress = lane.service
        root = f"/api/workspaces/{lane.world.ctx.tenant_id}/email-ingress"
        setup = lane.world.client.get(root + "/setup")
        assert setup.status_code == 200, setup.text
        assert setup.json()["enabled"] is True
        assert [row["id"] for row in setup.json()["email_connections"]] == [str(lane.email_id)]
        active = next(row for row in setup.json()["activations"] if row["id"] == str(lane.activation_id))
        invoice_ids = [lane.world.invoice_id]
        if scenario in {"multi_promise_reply", "multi_promise_amendment", "installment_promise_reply"}:
            anchor = lane.world.store.get(InvoiceSnapshot, lane.world.invoice_id)
            assert anchor is not None
            second = lane.world.store.scalar(
                select(InvoiceSnapshot)
                .where(
                    InvoiceSnapshot.tenant_id == anchor.tenant_id,
                    InvoiceSnapshot.customer_id == anchor.customer_id,
                    InvoiceSnapshot.id != anchor.id,
                )
                .order_by(InvoiceSnapshot.id)
            )
            assert second is not None
            invoice_ids.append(second.id)
            # End fixture reads before worker bootstrap creates concurrent checkpoint indexes.
            lane.world.store.rollback()
        command = {
            "request_id": str(uuid4()),
            "expected_activation_version": active["version"],
            "invoice_ids": [str(key) for key in invoice_ids],
            "scenario": scenario,
        }
        path = root + f"/activations/{lane.activation_id}/simulate"
        if scenario in {"multi_promise_reply", "multi_promise_amendment", "installment_promise_reply"}:
            refused = lane.world.client.post(
                path,
                json={**command, "request_id": str(uuid4()), "invoice_ids": [str(lane.world.invoice_id)]},
            )
            assert refused.status_code == 422
        response = lane.world.client.post(path, json=command)
        assert response.status_code == 202, response.text
        key = response.json()["id"]
        replay = lane.world.client.post(path, json=command)
        assert replay.status_code == 202 and replay.json()["id"] == key
        changed = lane.world.client.post(path, json={**command, "scenario": "unlinked_reply"})
        assert changed.status_code == 409, changed.text
        assert (
            lane.world.client.post(path, json={**command, "body": "arbitrary customer claim"}).status_code
            == 422
        )
        from aco.features.agent.ingress import produce_inbound_run
        from aco.features.agent.models import AgentRun

        asyncio.run(lane.work(produce_inbound_run))
        db = lane.world.store
        db.rollback()
        assert db.scalar(select(func.count()).select_from(NativeInboundMessage)) == 1
        assert db.scalar(select(func.count()).select_from(Communication)) == 1
        assert db.scalar(select(func.count()).select_from(CommunicationSource)) == 1
        assert db.scalar(select(func.count()).select_from(EmailIngressReceipt)) == 1
        assert db.scalar(select(func.count()).select_from(AgentRun)) == 1
        receipt = db.scalar(select(EmailIngressReceipt))
        assert receipt is not None and receipt.agent_run_id is not None and receipt.state == "processed"
        run = db.get(AgentRun, receipt.agent_run_id)
        assert run is not None and run.authenticity == "FAKE" and not run.provider_attempted
        record = db.scalar(select(Communication))
        assert record is not None and "SYNTHETIC" in record.content["text_body"]
        if scenario == "ambiguous_promise_reply":
            assert "I commit to pay AUD 40.00" in record.content["text_body"]
            assert "soon; the date and timezone are not yet agreed" in record.content["text_body"]
        if scenario == "promise_reply":
            assert "I commit to pay AUD 40.00" in record.content["text_body"]
            assert "in UTC." in record.content["text_body"]
        if scenario == "case_reply":
            assert "The purchase order reference is wrong" in record.content["text_body"]
        if scenario == "case_resolution":
            assert "The purchase order reference is now confirmed correct" in record.content["text_body"]
        if scenario in {"multi_promise_reply", "multi_promise_amendment", "installment_promise_reply"}:
            each = "75.00" if scenario == "multi_promise_amendment" else "100.00"
            for invoice_id in invoice_ids:
                invoice = db.get(InvoiceSnapshot, invoice_id)
                assert invoice is not None
                assert f"AUD {each} to {invoice.number}" in record.content["text_body"]
            assert "explicit invoice allocation" in record.content["text_body"]
            if scenario == "installment_promise_reply":
                assert "Installment 1: AUD 100.00 total" in record.content["text_body"]
                assert "Installment 2: AUD 100.00 total" in record.content["text_body"]
            assert record.origin_invoice_id in invoice_ids
        else:
            assert record.origin_invoice_id == lane.world.invoice_id
        assert record.source_state == "stored"
        lane.world.client.headers.pop("x-csrf-token", None)
        assert lane.world.client.post(path, json=command).status_code == 403


def test_fixed_unlinked_and_echo_scenarios_preserve_original_without_agent_and_secret_rotation_revokes() -> (
    None
):
    from aco.features.communications.ingress import EmailIngressService
    from aco.features.communications.models import CommunicationInvoiceLink

    with ingress_world() as lane:
        app = cast(FastAPI, lane.world.client.app)
        app.include_router(router)
        service = EmailIngressService(lane.service.sessions, enabled=True, default_secret=lane.secret)
        app.state.email_ingress = service
        root = f"/api/workspaces/{lane.world.ctx.tenant_id}/email-ingress"
        setup = lane.world.client.get(root + "/setup")
        assert setup.status_code == 200
        active = setup.json()["activations"][0]
        path = root + f"/activations/{active['id']}/simulate"
        for scenario in ("unlinked_reply", "outbound_echo"):
            command = {
                "request_id": str(uuid4()),
                "expected_activation_version": active["version"],
                "invoice_ids": [str(lane.world.invoice_id)],
                "scenario": scenario,
            }
            assert (
                lane.world.client.post(path, json={**command, "invoice_ids": [str(uuid4())]}).status_code
                == 403
            )
            assert (
                lane.world.client.post(path, json={**command, "expected_activation_version": 999}).status_code
                == 409
            )
            response = lane.world.client.post(path, json=command)
            assert response.status_code == 202, response.text
        asyncio.run(lane.work())
        db = lane.world.store
        db.rollback()
        assert db.scalar(select(func.count()).select_from(CommunicationSource)) == 2
        assert all(row.agent_run_id is None for row in db.scalars(select(EmailIngressReceipt)))
        unlinked = db.scalar(select(Communication).where(Communication.kind == "CUSTOMER_REPLY"))
        assert unlinked is not None and unlinked.origin_invoice_id is None
        assert (
            db.scalar(
                select(func.count())
                .select_from(CommunicationInvoiceLink)
                .where(CommunicationInvoiceLink.communication_id == unlinked.id)
            )
            == 0
        )
        service.default_secret = "whsec_cm90YXRlZC1zeW50aGV0aWMtc2VjcmV0"
        db.rollback()
        assert lane.world.client.get(root + "/setup").json()["activations"] == []
        assert lane.world.client.post(path, json=command).status_code == 403
        service.enabled = False
        assert lane.world.client.get(root + "/setup").json()["enabled"] is False
        assert lane.world.client.post(path, json=command).status_code == 403
