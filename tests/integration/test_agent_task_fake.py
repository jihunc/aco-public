"""Explicit offline objective uses genuine task reads and the maintained worker."""

import asyncio
import json
from uuid import UUID, uuid4

import pytest
from action_support import World, action_world
from evaluations.test_collection_agent import financial
from pydantic import SecretStr
from sqlalchemy import func, select

from aco.core.settings import Settings
from aco.features.agent.graph import dispatch_agent_runs
from aco.features.agent.models import AgentStep
from aco.features.review.models import CollectionTask
from aco.features.tools.schemas import ReadEvidence
from aco.features.tools.task_update import TaskFact
from aco.worker.app import WorkerRuntime


def fake_run(world: World, objective: str) -> dict[str, object]:
    path = f"/api/workspaces/{world.ctx.tenant_id}/agent-runs"
    started = world.client.post(
        path,
        json={"invoice_ids": [str(world.invoice_id)], "objective": objective, "request_id": str(uuid4())},
    )
    assert started.status_code == 202, started.text
    run_id = started.json()["id"]

    async def work() -> None:
        runtime = WorkerRuntime(Settings(database_url=SecretStr(world.url)))
        try:
            async with runtime.app.open_async():
                await dispatch_agent_runs(runtime.sessions, runtime.app)
                await asyncio.wait_for(
                    runtime.app.run_worker_async(
                        queues=["aco"], concurrency=1, wait=False, listen_notify=False
                    ),
                    timeout=35,
                )
        finally:
            await runtime.engine.dispose()

    world.store.rollback()
    asyncio.run(work())
    response = world.client.get(path + "/" + run_id)
    assert response.status_code == 200
    value: dict[str, object] = response.json()
    return value


@pytest.mark.parametrize("requested", ["current", "stale", "malformed"])
def test_explicit_fake_task_update_uses_observed_version_or_refuses(requested: str) -> None:
    with action_world() as world:
        path = f"/api/workspaces/{world.ctx.tenant_id}/tasks"
        created = world.client.post(
            path, json={"invoice_id": str(world.invoice_id), "title": "Retained source review", "priority": 3}
        )
        assert created.status_code == 201
        task = created.json()
        original_financial = financial(world)
        version = task["version"] if requested != "stale" else task["version"] + 1
        objective = f"Update task {task['id']} version {version} to done"
        if requested == "malformed":
            objective += "; also lower urgency"
        result = fake_run(world, objective)
        saved = world.client.get(path + "/" + task["id"]).json()
        assert result["authenticity"] == "FAKE" and result["provider_attempted"] is False
        assert result["status"] == ("completed" if requested == "current" else "failed"), result
        assert saved["status"] == ("done" if requested == "current" else "open")
        assert saved["version"] == task["version"] + (1 if requested == "current" else 0)
        for field in (
            "title",
            "notes",
            "priority",
            "priority_rank",
            "assignee_id",
            "due_at",
            "source_action_id",
        ):
            assert saved[field] == task[field]
        world.store.rollback()
        assert world.store.scalar(select(func.count()).select_from(CollectionTask)) == 1
        if requested != "malformed":
            step = world.store.scalar(
                select(AgentStep).where(
                    AgentStep.run_id == UUID(str(result["id"])),
                    AgentStep.tool_name == "list_collection_tasks",
                )
            )
            assert step is not None and step.status == "completed" and step.output is not None
            evidence = ReadEvidence.model_validate_json(json.dumps(step.output))
            assert len(evidence.records) == 1
            observed = evidence.records[0]
            assert isinstance(observed, TaskFact)
            assert observed.task_id == UUID(task["id"])
            assert observed.version == task["version"]
        assert financial(world) == original_financial
