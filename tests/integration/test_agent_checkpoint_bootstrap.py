"""First-use maintained checkpoint setup must finish before a run's authority lease starts."""

import asyncio
from uuid import UUID, uuid4

import pytest
from action_support import action_world
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from pydantic import SecretStr

from aco.core.settings import Settings
from aco.features.agent.graph import dispatch_agent_runs
from aco.features.agent.models import AgentRun
from aco.worker.app import WorkerRuntime


@pytest.mark.parametrize("setup_failure", [False, True])
async def test_actual_worker_does_not_spend_run_lease_waiting_for_checkpoint_bootstrap(
    monkeypatch: pytest.MonkeyPatch, setup_failure: bool
) -> None:
    with action_world() as world:
        response = world.client.post(
            f"/api/workspaces/{world.ctx.tenant_id}/agent-runs",
            json={
                "invoice_ids": [str(world.invoice_id)],
                "objective": "Inspect imported invoice evidence",
                "request_id": str(uuid4()),
            },
        )
        assert response.status_code == 202
        run_id = UUID(response.json()["id"])
        world.store.rollback()
        entered, release = asyncio.Event(), asyncio.Event()
        setup = AsyncPostgresSaver.setup

        async def paused_setup(saver: AsyncPostgresSaver) -> None:
            entered.set()
            await release.wait()
            if setup_failure:
                raise RuntimeError("Synthetic checkpoint setup failure")
            await setup(saver)

        monkeypatch.setattr(AsyncPostgresSaver, "setup", paused_setup)
        runtime = WorkerRuntime(Settings(database_url=SecretStr(world.url)))
        try:
            async with runtime.app.open_async():
                await dispatch_agent_runs(runtime.sessions, runtime.app)
                worker = asyncio.create_task(
                    runtime.app.run_worker_async(
                        queues=["aco"], concurrency=1, wait=False, listen_notify=False
                    )
                )
                try:
                    await asyncio.wait_for(entered.wait(), 10)
                    world.store.rollback()
                    run = world.store.get(AgentRun, run_id, populate_existing=True)
                    assert run is not None
                    assert run.status == "queued" and run.lease_token is None and run.lease_until is None
                    assert run.model_state == "not_started" and run.provider_attempted is False
                finally:
                    world.store.rollback()
                    release.set()
                    await asyncio.wait_for(worker, 30)
        finally:
            await runtime.engine.dispose()
        world.store.rollback()
        finished = world.store.get(AgentRun, run_id, populate_existing=True)
        assert finished is not None
        assert finished.status == ("failed" if setup_failure else "completed"), finished.error_code
        assert finished.provider_attempted is False
        if setup_failure:
            assert finished.error_code == "CHECKPOINT_BOOTSTRAP_UNAVAILABLE"
            assert finished.lease_token is None and finished.model_state == "not_started"
