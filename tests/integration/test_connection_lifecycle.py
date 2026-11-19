"""Owner disconnect fences actual queued AI and signed voice tools, retaining history."""

import time
from uuid import uuid4

from action_support import World, action_world, workers
from sqlalchemy import func, select
from voice_support import exercise_voice

from aco.features.agent.models import AgentRun
from aco.features.identity.models import ProviderConnection
from aco.features.review.models import CollectionTask
from aco.features.usage.models import ProviderUsageEvent


def wait_terminal(world: World, path: str) -> dict[str, object]:
    deadline = time.monotonic() + 25
    while True:
        response = world.client.get(path)
        assert response.status_code == 200, response.text
        value: dict[str, object] = response.json()
        if value["status"] not in {"queued", "running"}:
            return value
        assert time.monotonic() < deadline, value
        time.sleep(0.1)


def test_llm_disconnect_retains_completed_history_and_fences_queued_and_new_runs() -> None:
    with action_world() as world:
        base = f"/api/workspaces/{world.ctx.tenant_id}"
        body = {
            "invoice_ids": [str(world.invoice_id)],
            "objective": "Create an internal follow-up task from verified invoice evidence",
        }
        first = world.client.post(base + "/agent-runs", json=body | {"request_id": str(uuid4())})
        assert first.status_code == 202, first.text
        first_path = base + "/agent-runs/" + first.json()["id"]
        with workers(world, crash="domain"):
            completed = wait_terminal(world, first_path)
        assert completed["execution"] == "EXECUTED", completed
        history = world.client.get(first_path + "/steps").json()
        usage_ids = list(
            world.store.scalars(select(ProviderUsageEvent.id).where(ProviderUsageEvent.provider == "mock"))
        )
        assert usage_ids, "Actual completed fake run must retain its canonical model usage event"
        task_ids = list(world.store.scalars(select(CollectionTask.id)))
        world.store.rollback()
        queued = world.client.post(base + "/agent-runs", json=body | {"request_id": str(uuid4())})
        assert queued.status_code == 202, queued.text
        connections = world.client.get(base + "/provider-connections").json()
        connection = next(row for row in connections if row["provider_type"] == "llm")
        disconnected = world.client.post(
            base + f"/provider-connections/{connection['id']}/disconnect",
            json={"expected_version": connection["version"]},
        )
        assert disconnected.status_code == 200, disconnected.text
        assert disconnected.json()["remote_revocation"] == "UNKNOWN"
        with workers(world, crash="domain"):
            failed = wait_terminal(world, base + "/agent-runs/" + queued.json()["id"])
        assert failed["status"] == "failed", failed
        assert failed["provider_attempted"] is False
        assert world.client.get(first_path).json() == completed
        assert world.client.get(first_path + "/steps").json() == history
        denied = world.client.post(base + "/agent-runs", json=body | {"request_id": str(uuid4())})
        assert denied.status_code == 409, denied.text
        assert denied.json()["detail"] == "LLM_CONNECTION_UNAVAILABLE"
        world.store.rollback()
        assert list(world.store.scalars(select(CollectionTask.id))) == task_ids
        assert (
            list(
                world.store.scalars(
                    select(ProviderUsageEvent.id).where(ProviderUsageEvent.provider == "mock")
                )
            )
            == usage_ids
        )
        assert world.store.scalar(select(func.count()).select_from(AgentRun)) == 2
        assert (
            world.store.scalar(
                select(func.count())
                .select_from(ProviderConnection)
                .where(ProviderConnection.provider_type == "llm")
            )
            == 1
        )


async def test_voice_disconnect_preserves_native_call_and_usage_but_refuses_new_tool() -> None:
    await exercise_voice("connection_disconnected")


async def test_inbound_does_not_recreate_disconnected_mock_connection() -> None:
    from ingress_support import ingress_world

    from aco.features.agent.ingress import produce_inbound_run
    from aco.features.communications.models import EmailIngressReceipt

    with ingress_world() as lane:
        db = lane.world.store
        connection = ProviderConnection(
            tenant_id=lane.world.ctx.tenant_id, provider_type="llm", provider="mock", status="DISCONNECTED"
        )
        db.add(connection)
        db.commit()
        receipt_id = await lane.deliver(lane.message())
        db.rollback()
        await lane.work(produce_inbound_run)
        db.rollback()
        receipt = db.get(EmailIngressReceipt, receipt_id)
        assert receipt is not None and receipt.state == "review_required"
        assert receipt.error_code == "LLM_CONNECTION_UNAVAILABLE"
        assert receipt.communication_id is not None and receipt.agent_run_id is None
        assert db.scalar(select(func.count()).select_from(AgentRun)) == 0
        assert (
            db.scalar(
                select(func.count())
                .select_from(ProviderConnection)
                .where(ProviderConnection.provider_type == "llm")
            )
            == 1
        )
