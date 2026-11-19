"""Actual composed API/maintained worker/private APP capture; all sources explicitly synthetic."""

from typing import cast
from uuid import UUID, uuid4

import pytest
from artifact_support import storage_configuration
from fastapi import FastAPI
from ingress_support import IngressWorld, ingress_world
from pydantic import SecretStr
from sqlalchemy import func, select
from starlette.testclient import TestClient

from aco.api.app import create_app
from aco.core.settings import Settings
from aco.features.agent.models import AgentRun
from aco.features.artifacts.models import Artifact
from aco.features.communications.models import Communication, EmailIngressReceipt
from aco.features.review.models import CollectionTask
from aco.worker.app import WorkerRuntime


def configured(lane: IngressWorld, **changes: object) -> Settings:
    original = cast(FastAPI, lane.world.client.app).state.settings
    storage = storage_configuration()
    values = (
        original.model_dump()
        | {
            "synthetic_email_ingress_enabled": True,
            "synthetic_email_ingress_webhook_secret": SecretStr(lane.secret),
            "storage_endpoint": storage["endpoint"],
            "storage_bucket": storage["bucket"],
            "storage_access_key": storage["access_key"],
            "storage_secret_key": storage["secret_key"],
            "storage_local_endpoint_ip": storage.get("local_endpoint_ip"),
        }
        | changes
    )
    return Settings.model_validate(values)


async def drain(runtime: WorkerRuntime) -> None:
    try:
        async with runtime.app.open_async():
            for tick in (1, 2):
                await runtime.app.tasks["aco.maintenance"].defer_async(timestamp=tick)
                await runtime.app.run_worker_async(
                    queues=["aco"], concurrency=1, wait=False, listen_notify=False
                )
    finally:
        await runtime.engine.dispose()


async def test_composed_owner_api_reaches_private_original_fake_run_and_task() -> None:
    with ingress_world() as lane:
        settings = configured(lane)
        app = create_app(settings)
        app.state.identity.oauth_transport = cast(
            FastAPI, lane.world.client.app
        ).state.identity.oauth_transport
        client = TestClient(app, base_url="http://127.0.0.1:53000")
        client.cookies.update(lane.world.client.cookies)
        client.headers.update(lane.world.client.headers)
        root = f"/api/workspaces/{lane.world.ctx.tenant_id}/email-ingress"
        setup = client.get(root + "/setup")
        assert setup.status_code == 200, setup.text
        assert setup.json()["enabled"] is True
        active = setup.json()["activations"][0]
        command = {
            "request_id": str(uuid4()),
            "expected_activation_version": active["version"],
            "invoice_ids": [str(lane.world.invoice_id)],
            "scenario": "invoice_reply",
        }
        response = client.post(root + f"/activations/{active['id']}/simulate", json=command)
        assert response.status_code == 202, response.text
        receipt_id = UUID(response.json()["id"])
        lane.world.store.rollback()
        await drain(WorkerRuntime(settings))
        db = lane.world.store
        db.rollback()
        receipt = db.get(EmailIngressReceipt, receipt_id)
        assert receipt is not None and receipt.state == "processed", receipt.error_code if receipt else None
        run = db.get(AgentRun, receipt.agent_run_id)
        assert run is not None and run.status == "completed", run.error_code if run else None
        assert run.authenticity == "FAKE" and not run.provider_attempted
        assert db.scalar(select(func.count()).select_from(Communication)) == 1
        assert db.scalar(select(func.count()).select_from(Artifact)) == 1
        task_count = db.scalar(select(func.count()).select_from(CollectionTask))
        assert task_count is not None and task_count >= 1
        assert client.get(root + f"/receipts/{receipt_id}").json()["agent_run_id"] == str(run.id)
        assert client.get(f"/api/workspaces/{uuid4()}/email-ingress/setup").status_code == 403
        client.headers.pop("x-csrf-token")
        assert client.post(root + f"/activations/{active['id']}/simulate", json=command).status_code == 403
        app.state.sessions.kw["bind"].dispose()
        await app.state.business_sessions.kw["bind"].dispose()


@pytest.mark.parametrize(
    "changed,expected",
    [
        ("disabled", "SYNTHETIC_INGRESS_DISABLED"),
        ("secret", "INGRESS_CONFIGURATION_CHANGED"),
        ("storage", "INGRESS_STORAGE_UNAVAILABLE"),
        ("revoked", "INGRESS_ACTIVATION_REVOKED"),
    ],
)
async def test_queued_receipt_cannot_capture_after_runtime_or_activation_change(
    changed: str, expected: str
) -> None:
    from aco.features.communications.models import EmailIngressActivation
    from aco.features.identity.models import now

    with ingress_world() as lane:
        message = lane.message()
        lane.world.store.rollback()
        key = await lane.deliver(message)
        updates: dict[str, object] = {}
        if changed == "disabled":
            updates["synthetic_email_ingress_enabled"] = False
        elif changed == "secret":
            updates["synthetic_email_ingress_webhook_secret"] = SecretStr("rotated-synthetic-secret")
        elif changed == "storage":
            updates["storage_access_key"] = None
        else:
            activation = lane.world.store.get(EmailIngressActivation, lane.activation_id)
            assert activation is not None
            activation.revoked_at = now()
            lane.world.store.commit()
        settings = configured(lane, **updates)
        app = create_app(settings)
        app.state.identity.oauth_transport = cast(
            FastAPI, lane.world.client.app
        ).state.identity.oauth_transport
        client = TestClient(app, base_url="http://127.0.0.1:53000")
        client.cookies.update(lane.world.client.cookies)
        client.headers.update(lane.world.client.headers)
        setup = client.get(f"/api/workspaces/{lane.world.ctx.tenant_id}/email-ingress/setup")
        assert setup.status_code == 200
        assert setup.json()["enabled"] is (changed not in {"disabled", "storage"})
        if changed in {"secret", "revoked"}:
            assert setup.json()["activations"] == []
        app.state.sessions.kw["bind"].dispose()
        await app.state.business_sessions.kw["bind"].dispose()
        lane.world.store.rollback()
        await drain(WorkerRuntime(settings))
        db = lane.world.store
        db.rollback()
        receipt = db.get(EmailIngressReceipt, key)
        assert receipt is not None and receipt.state == "failed"
        assert receipt.error_code == expected
        assert db.scalar(select(func.count()).select_from(Communication)) == 0
        assert db.scalar(select(func.count()).select_from(Artifact)) == 0
        assert db.scalar(select(func.count()).select_from(AgentRun)) == 0


def test_outbound_dispatch_keeps_its_canonical_thread_when_inbound_thread_exists() -> None:
    from reminder_support import dispatch_one, reminder_world

    from aco.features.communications.models import (
        CommunicationThread,
        EmailDispatchBinding,
        EmailExecutionActivation,
    )

    with reminder_world() as (client, db, ctx, invoices, _saved):
        activation = db.scalar(select(EmailExecutionActivation))
        assert activation is not None
        for invoice in invoices:
            db.add(
                CommunicationThread(
                    tenant_id=ctx.tenant_id,
                    provider_connection_id=activation.email_connection_id,
                    origin_invoice_id=invoice.id,
                    customer_id=invoice.customer_id,
                    native_thread_id=f"simulated-inbound:{uuid4()}",
                    purpose="provider_inbound",
                )
            )
        db.commit()
        action = dispatch_one(client, db, ctx)
        assert action.status == "succeeded", action.last_error
        binding = db.scalar(select(EmailDispatchBinding).where(EmailDispatchBinding.action_id == action.id))
        assert binding is not None
        message = db.get(Communication, binding.communication_id)
        assert message is not None
        thread = db.get(CommunicationThread, message.thread_id)
        assert thread is not None and thread.purpose == "invoice_outbound"
