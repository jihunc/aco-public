"""Delayed original timestamps reach real SDK reads; no worker-clock date inference."""

import asyncio
import json
from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx2 as httpx
import pytest
from evaluations.test_collection_agent import promise, proposal, run_sdk
from ingress_support import ingress_world
from sqlalchemy import select

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.agent.models import AgentRun, AgentStep
from aco.features.communications.models import Communication, EmailIngressReceipt
from aco.features.identity.models import now
from aco.features.promises.models import PaymentPromise


@pytest.mark.parametrize("resolved", [False, True], ids=["ambiguous-pending", "explicit-human-resolution"])
def test_delayed_original_date_context_never_uses_worker_now(
    monkeypatch: pytest.MonkeyPatch, resolved: bool
) -> None:
    bodies: list[dict[str, Any]] = []
    actual_http = httpx.MockTransport.handle_async_request

    async def observed_http(transport: httpx.MockTransport, request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return await actual_http(transport, request)

    monkeypatch.setattr(httpx.MockTransport, "handle_async_request", observed_http)
    original_at = (now() - timedelta(days=10)).replace(hour=0, minute=30, second=0, microsecond=0)
    intended_timezone = "America/Los_Angeles"
    local_source_day = original_at.astimezone(ZoneInfo(intended_timezone)).date()
    explicit_date = local_source_day + timedelta(days=1)
    assert explicit_date == original_at.date()  # The source local day is the previous UTC date.
    assert now() > original_at + timedelta(days=7)
    wording = (
        "Synthetic incoming INV-101 commitment: I will pay AUD 100 tomorrow, "
        "meaning the next calendar day in America/Los_Angeles."
        if resolved
        else "Synthetic incoming INV-101 commitment: I will pay AUD 100 next Friday."
    )
    with ingress_world() as lane:
        world, db = lane.world, lane.world.store
        invoice = db.get(InvoiceSnapshot, world.invoice_id)
        assert invoice is not None
        financial = (invoice.source_revision, invoice.balance_minor, invoice.financial_status)
        native = lane.message(body=wording).model_copy(update={"received_at": original_at})
        db.rollback()

        async def capture() -> UUID:
            key = await lane.deliver(native)
            assert isinstance(key, UUID)
            await lane.work(None)
            return key

        receipt_id = asyncio.run(capture())
        db.rollback()
        receipt = db.get(EmailIngressReceipt, receipt_id)
        assert receipt is not None and receipt.communication_id is not None
        message = db.get(Communication, receipt.communication_id)
        assert message is not None and message.source_state == "stored"
        assert message.event_at == original_at and message.content["text_body"] == wording
        command = promise(
            invoice,
            message,
            due_date=explicit_date.isoformat() if resolved else None,
            timezone=intended_timezone if resolved else None,
        )
        # The HTTP model double supplies typed terms; this tests application execution,
        # source propagation and guards, not genuine model judgement or live inference.
        saved = run_sdk(
            world,
            [invoice],
            [proposal(invoice, "record_promise", promise_command=command)],
            extra_reads=[("get_communication_history", invoice.id)],
        )
        assert saved["status"] == "completed", saved
        db.expire_all()
        row = db.scalar(select(PaymentPromise))
        assert row is not None and row.source_at == original_at
        assert row.source_text == wording and row.source_communication_id == message.id
        assert row.status == "NEEDS_CONFIRMATION"
        run = db.get(AgentRun, UUID(saved["id"]))
        assert run is not None
        assert run.prompt_version == "investigation-v13" and run.schema_version == "investigation-v13"
        assert run.created_at > original_at + timedelta(days=7)
        read = db.scalar(
            select(AgentStep).where(
                AgentStep.run_id == run.id,
                AgentStep.tool_name == "get_communication_history",
                AgentStep.status == "completed",
            )
        )
        assert read is not None and read.output is not None
        records = read.output["records"]
        assert isinstance(records, list)
        observed = next(fact for fact in records if fact["communication_id"] == str(message.id))
        assert datetime.fromisoformat(observed["event_at"].replace("Z", "+00:00")) == original_at
        base = f"/api/workspaces/{world.ctx.tenant_id}/promises/{row.id}"
        confirmation = {
            "promise_id": str(row.id),
            "expected_version": row.version,
            "confirmation_evidence": command["evidence"],
            "reason": "Current human review of original source date and explicit intended timezone.",
        }
        db.rollback()
        response = world.client.post(base + "/confirm", json=confirmation)
        if resolved:
            assert response.status_code == 200, response.text
            value = response.json()
            assert value["status"] == "ACTIVE" and value["due_date"] == explicit_date.isoformat()
            assert value["timezone"] == intended_timezone
            assert datetime.fromisoformat(value["cutoff_at"].replace("Z", "+00:00")) == datetime.combine(
                explicit_date + timedelta(days=1), time.min, ZoneInfo(intended_timezone)
            ).astimezone(UTC)
            assert datetime.fromisoformat(value["source_at"].replace("Z", "+00:00")) == original_at
        else:
            assert response.status_code == 409 and response.json()["detail"] == "PROMISE_TERMS_UNRESOLVED"
            current = world.client.get(base).json()
            assert current["status"] == "NEEDS_CONFIRMATION"
            assert (
                current["due_date"] is None and current["timezone"] is None and current["cutoff_at"] is None
            )
            assert {"date_unresolved", "timezone_unresolved"} <= set(current["confirmation_reasons"])
            assert current["scheduling_status"] == "PENDING_CONFIRMATION"
        db.rollback()
        current_invoice = db.get(InvoiceSnapshot, invoice.id, populate_existing=True)
        assert current_invoice is not None
        assert (
            current_invoice.source_revision,
            current_invoice.balance_minor,
            current_invoice.financial_status,
        ) == financial
        assert len(bodies) == 2
        # Assert instructions on the actual SDK wire, not just a constant or mocked node.
        wire = json.dumps(bodies[0])
        assert "Communication.event_at" in wire
        assert "never the worker clock" in wire
        assert "no implicit tenant-timezone fallback" in wire
        assert "leave due_date/timezone unresolved" in wire
