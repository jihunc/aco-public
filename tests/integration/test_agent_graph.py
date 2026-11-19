"""Connected scoped investigation evidence, real PostgreSQL/worker/checkpoints, fake model."""

import time
from uuid import UUID, uuid4

from action_support import action_world, workers


def test_manual_investigation_runs_in_maintained_worker_and_persists_tools() -> None:
    with action_world() as world:
        path = f"/api/workspaces/{world.ctx.tenant_id}/agent-runs"
        response = world.client.post(
            path,
            json={
                "invoice_ids": [str(world.invoice_id)],
                "objective": "Investigate balance and verified payment evidence",
                "request_id": str(uuid4()),
            },
        )
        assert response.status_code == 202, response.text
        run = response.json()
        with workers(world, crash="domain"):
            deadline = time.monotonic() + 25
            while run["status"] in {"queued", "running"}:
                assert time.monotonic() < deadline, run
                time.sleep(0.1)
                run = world.client.get(path + "/" + run["id"]).json()
        assert run["status"] == "completed", str(run) + str(
            world.client.get(path + "/" + run["id"] + "/steps").json()
        )
        assert run["authenticity"] == "FAKE"
        assert run["provider_attempted"] is False
        assert run["result"]["invoice_findings"][0]["invoice_id"] == str(world.invoice_id)
        proposals = run["result"]["proposals"]
        assert len(proposals) == 1
        assert proposals[0]["executed"] and proposals[0]["kind"] == "request_human_review"
        task = world.client.get(
            f"/api/workspaces/{world.ctx.tenant_id}/tasks/{proposals[0]['changed_ids'][0]}"
        )
        assert task.status_code == 200
        assert task.json()["assignee_id"] == str(world.ctx.actor_id)
        assert task.json()["due_at"] is None
        steps = world.client.get(path + "/" + run["id"] + "/steps").json()
        assert {s["tool_name"] for s in steps["items"]} >= {"get_invoice", "get_verified_payments"}
        events = world.client.get(path + "/" + run["id"] + "/event-log").json()
        sequences = [e["event_sequence"] for e in events["items"]]
        assert sequences == list(range(1, len(sequences) + 1))
        assert any(e["kind"] == "step.started" for e in events["items"])
        assert any(e["kind"] == "step.completed" for e in events["items"])
        assert (
            world.client.get(path + "/" + run["id"] + f"/event-log?after={sequences[-1]}").json()["items"]
            == []
        )


def test_investigation_creates_guarded_canonical_task_and_action() -> None:
    from sqlalchemy import select

    from aco.features.actions.models import ActionExecution, ScheduledAction
    from aco.features.review.models import CollectionTask

    with action_world() as world:
        path = f"/api/workspaces/{world.ctx.tenant_id}/agent-runs"
        response = world.client.post(
            path,
            json={
                "invoice_ids": [str(world.invoice_id)],
                "objective": "Create an internal follow-up task from verified invoice evidence",
                "request_id": str(uuid4()),
            },
        )
        assert response.status_code == 202, response.text
        run = response.json()
        with workers(world, crash="domain"):
            deadline = time.monotonic() + 25
            while run["status"] in {"queued", "running"}:
                assert time.monotonic() < deadline, run
                time.sleep(0.1)
                run = world.client.get(path + "/" + run["id"]).json()
        assert run["execution"] == "EXECUTED", run
        world.store.rollback()
        task = world.store.scalar(select(CollectionTask).where(CollectionTask.invoice_id == world.invoice_id))
        assert task is not None
        action = world.store.get(ScheduledAction, task.source_action_id)
        assert action is not None
        assert action.kind == "agent_internal" and action.agent_run_id == UUID(run["id"])
        assert action.source_event_id is None and task.source_event_id is None
        execution = world.store.scalar(select(ActionExecution).where(ActionExecution.action_id == action.id))
        assert execution is not None and execution.status == "succeeded"


def test_scope_role_csrf_and_cancel_are_current_and_no_work_is_replayed() -> None:
    from sqlalchemy import select

    from aco.features.agent.models import AgentStep
    from aco.features.identity.models import Membership

    with action_world() as world:
        path = f"/api/workspaces/{world.ctx.tenant_id}/agent-runs"
        body = {
            "invoice_ids": [str(world.invoice_id)],
            "objective": "Inspect without changing financial state",
            "request_id": str(uuid4()),
        }
        assert world.client.post(path, json=body | {"tenant_id": str(uuid4())}).status_code == 422
        assert world.client.post(path, json=body | {"invoice_ids": [str(uuid4())]}).status_code == 422
        csrf = world.client.headers.pop("x-csrf-token")
        assert world.client.post(path, json=body).status_code == 403
        world.client.headers["x-csrf-token"] = csrf
        row = world.store.scalar(select(Membership).where(Membership.tenant_id == world.ctx.tenant_id))
        assert row is not None
        row.role = "viewer"
        row.version += 1
        world.store.commit()
        assert world.client.post(path, json=body).status_code == 403
        row.role = "owner"
        row.version += 1
        world.store.commit()
        response = world.client.post(path, json=body)
        assert response.status_code == 202, response.text
        run = response.json()
        assert world.client.post(path, json=body).json()["id"] == run["id"]
        assert world.client.post(path, json=body | {"objective": "changed"}).status_code == 409
        assert (
            world.client.post(path + "/" + run["id"] + "/cancel", json={"expected_version": 99}).status_code
            == 409
        )
        assert (
            world.client.post(
                path + "/" + run["id"] + "/cancel", json={"expected_version": run["version"]}
            ).json()["status"]
            == "cancelled"
        )
        assert (
            world.client.get(
                path + "/" + run["id"] + "/events", headers={"Last-Event-ID": str(uuid4()) + ":1"}
            ).status_code
            == 422
        )
        replay = world.client.get(path + "/" + run["id"] + "/events")
        assert replay.status_code == 200 and "event: cancelled" in replay.text
        world.store.rollback()
        assert world.store.scalar(select(AgentStep).where(AgentStep.run_id == UUID(run["id"]))) is None
        row = world.store.scalar(select(Membership).where(Membership.tenant_id == world.ctx.tenant_id))
        assert row is not None
        row.active = False
        row.version += 1
        world.store.commit()
        assert world.client.get(path + "/" + run["id"]).status_code == 403
        assert world.client.get(path + "/" + run["id"] + "/event-log").status_code == 403


def test_revoked_or_uncertain_run_cannot_start_model_or_tools() -> None:
    from sqlalchemy import select

    from aco.features.agent.models import AgentRun, AgentStep

    with action_world() as world:
        path = f"/api/workspaces/{world.ctx.tenant_id}/agent-runs"
        ids = []
        for objective in ("Uncertain model", "Revoked membership"):
            r = world.client.post(
                path,
                json={
                    "invoice_ids": [str(world.invoice_id)],
                    "objective": objective,
                    "request_id": str(uuid4()),
                },
            )
            assert r.status_code == 202, r.text
            ids.append(UUID(r.json()["id"]))
        world.store.rollback()
        uncertain = world.store.get(AgentRun, ids[0])
        assert uncertain is not None
        uncertain.model_state = "intent"
        revoked = world.store.get(AgentRun, ids[1])
        assert revoked is not None
        revoked.membership_version -= 1
        world.store.commit()
        with workers(world, crash="domain"):
            deadline = time.monotonic() + 25
            while True:
                world.store.rollback()
                runs = [world.store.get(AgentRun, k, populate_existing=True) for k in ids]
                if all(r is not None and r.status == "failed" for r in runs):
                    break
                assert time.monotonic() < deadline
                time.sleep(0.1)
        assert {r.error_code for r in runs if r is not None} == {
            "MODEL_ATTEMPT_UNCERTAIN_NO_REPLAY",
            "RUN_AUTHORITY_REVOKED",
        }
        assert world.store.scalar(select(AgentStep).where(AgentStep.run_id.in_(ids))) is None


def crash_after_agent_action_commit(url: str) -> None:
    """Fault injection only at the business-commit/checkpoint gap in a real child worker."""
    import asyncio
    import os

    from pydantic import SecretStr

    from aco.core.settings import Settings
    from aco.features.agent.nodes import InvestigationNode
    from aco.features.agent.schemas import GraphState
    from aco.worker.app import WorkerRuntime

    original = InvestigationNode.execute

    async def crash(self: InvestigationNode, state: GraphState) -> GraphState:
        await original(self, state)
        os._exit(73)

    from pytest import MonkeyPatch

    with MonkeyPatch.context() as patch:
        patch.setattr(InvestigationNode, "execute", crash)
        asyncio.run(WorkerRuntime(Settings(database_url=SecretStr(url))).run())


def test_worker_death_after_action_commit_recovers_without_duplicate_action_or_model() -> None:
    import multiprocessing

    from sqlalchemy import func, select

    from aco.features.actions.models import ActionExecution, ScheduledAction
    from aco.features.agent.models import AgentRun, AgentStep
    from aco.features.review.models import CollectionTask

    with action_world() as world:
        path = f"/api/workspaces/{world.ctx.tenant_id}/agent-runs"
        response = world.client.post(
            path,
            json={
                "invoice_ids": [str(world.invoice_id)],
                "objective": "Create an internal task",
                "request_id": str(uuid4()),
            },
        )
        assert response.status_code == 202, response.text
        run_id = UUID(response.json()["id"])
        world.store.rollback()
        process = multiprocessing.get_context("spawn").Process(
            target=crash_after_agent_action_commit, args=(world.url,)
        )
        process.start()
        try:
            process.join(timeout=30)
            assert process.exitcode == 73
        finally:
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
        world.store.rollback()
        interrupted = world.store.get(AgentRun, run_id)
        assert interrupted is not None and interrupted.status == "running"
        assert interrupted.model_state == "complete"
        with workers(world, crash="domain"):
            deadline = time.monotonic() + 30
            while True:
                run = world.client.get(f"{path}/{run_id}").json()
                if run["status"] not in {"queued", "running"}:
                    break
                assert time.monotonic() < deadline, run
                time.sleep(0.1)
        assert run["status"] == "completed", run
        assert run["execution"] == "EXECUTED"
        world.store.rollback()
        recovered = world.store.get(AgentRun, run_id)
        assert recovered is not None and recovered.incarnation == 2
        assert (
            world.store.scalar(
                select(func.count())
                .select_from(CollectionTask)
                .where(CollectionTask.source_agent_run_id == run_id)
            )
            == 1
        )
        actions = list(
            world.store.scalars(select(ScheduledAction).where(ScheduledAction.agent_run_id == run_id))
        )
        assert len(actions) == 1 and actions[0].status == "succeeded"
        assert (
            world.store.scalar(
                select(func.count())
                .select_from(ActionExecution)
                .where(ActionExecution.action_id == actions[0].id)
            )
            == 1
        )
        steps = list(world.store.scalars(select(AgentStep).where(AgentStep.run_id == run_id)))
        assert sum(s.kind == "model" for s in steps) == 1
        assert sum(s.kind == "action" for s in steps) == 1


def test_invoice_bound_service_authority_cannot_read_or_start_human_run_history() -> None:
    from typing import cast

    import pytest
    from action_support import enqueue
    from fastapi import HTTPException
    from sqlalchemy import select

    from aco.core.context import TenantContext
    from aco.features.accounting.models import InvoiceSnapshot
    from aco.features.actions.models import ScheduledAction
    from aco.features.actions.service import worker_context
    from aco.features.agent.schemas import AgentTrigger
    from aco.features.agent.service import AgentService
    from aco.features.identity.service import IdentityService, Permission

    with action_world() as world:
        second = world.store.scalar(
            select(InvoiceSnapshot).where(
                InvoiceSnapshot.tenant_id == world.ctx.tenant_id, InvoiceSnapshot.id != world.invoice_id
            )
        )
        assert second is not None
        service = AgentService(world.store)
        target = service.start(
            world.ctx,
            AgentTrigger(
                invoice_ids=[second.id], objective="Other invoice's private investigation", request_id=uuid4()
            ),
        )
        world.store.commit()
        ref = enqueue(world)
        action = world.store.get(ScheduledAction, ref.action_id)
        assert action is not None
        bound = worker_context(action)
        # This is valid narrow invoice authority, not a deliberately malformed context.
        IdentityService(world.store).authorize(bound, Permission.INVOICE_READ)
        invalid_human = cast(TenantContext, bound)
        for operation in (
            lambda: service.list(invalid_human),
            lambda: service.locked(invalid_human, target.id),
            lambda: service.steps(invalid_human, target.id),
            lambda: service.events(invalid_human, target.id, 0),
            lambda: service.start(
                invalid_human,
                AgentTrigger(invoice_ids=[second.id], objective="Forbidden service run", request_id=uuid4()),
            ),
            lambda: service.cancel(invalid_human, target.id, target.version),
        ):
            with pytest.raises(HTTPException) as denied:
                operation()
            assert denied.value.status_code == 403
            assert denied.value.detail == "HUMAN_REQUIRED"
        assert service.locked(world.ctx, target.id).id == target.id
