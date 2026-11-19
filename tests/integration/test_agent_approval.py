"""Real maintained graph interruption and independent approval of an internal task."""

import time
from typing import Any
from uuid import UUID, uuid4

import pytest
from action_support import World, action_world, workers
from fastapi import FastAPI
from identity_support import add_actor
from sqlalchemy import func, select

from aco.core.context import TenantContext
from aco.core.types import Role
from aco.features.actions.models import ActionExecution
from aco.features.agent.models import AgentStep
from aco.features.identity.models import Membership
from aco.features.review.approval_dto import ApprovalDecisionCommand
from aco.features.review.approvals import HumanApprovalService
from aco.features.review.models import ApprovalRequest, CollectionTask


def wait_run(world: World, run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 25
    while True:
        response = world.client.get(f"/api/workspaces/{world.ctx.tenant_id}/agent-runs/{run_id}")
        assert response.status_code == 200, response.text
        value: dict[str, Any] = response.json()
        if value["status"] not in {"queued", "running"}:
            return value
        assert time.monotonic() < deadline, value
        time.sleep(0.1)


def independent_reviewer(world: World) -> TenantContext:
    application = world.client.app
    assert isinstance(application, FastAPI)
    user, _ = add_actor(world.store, application, "independent@synthetic.invalid")
    world.store.add(Membership(tenant_id=world.ctx.tenant_id, user_id=user.id, role="finance_manager"))
    world.store.commit()
    context = world.ctx.model_copy(update={"actor_id": user.id, "role": Role.FINANCE_MANAGER})
    assert isinstance(context, TenantContext)
    return context


def test_real_graph_waits_then_independent_decision_resumes_exact_task_once() -> None:
    with action_world() as world:
        response = world.client.post(
            f"/api/workspaces/{world.ctx.tenant_id}/agent-runs",
            json={
                "invoice_ids": [str(world.invoice_id)],
                "objective": "Request approval before creating an internal task",
                "request_id": str(uuid4()),
            },
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["id"]
        with workers(world, crash="domain"):
            run = wait_run(world, run_id)
        assert run["status"] == "awaiting_approval", run
        world.store.rollback()
        assert world.store.scalar(select(func.count()).select_from(CollectionTask)) == 0
        request = world.store.scalar(select(ApprovalRequest))
        assert request is not None
        request_id = request.id
        ctx = independent_reviewer(world)
        service = HumanApprovalService(world.store)
        view = service.get(ctx, request_id)
        assert view.can_approve and view.effect == "internal_task"
        assert view.dispatch_status == "NOT_APPLICABLE"
        assert view.agent_evidence and view.source_run_id == UUID(run_id)
        service.decide_approval(
            ctx,
            request_id,
            ApprovalDecisionCommand(
                expected_version=view.version,
                reviewed_action_version=view.action_version,
                reviewed_material_hash=view.material_hash,
                reviewed_binding_hash=view.current_binding_hash,
                decision="approved",
                reason="Reviewed exact internal task and source evidence",
            ),
        )
        world.store.commit()
        import multiprocessing

        process = multiprocessing.get_context("spawn").Process(
            target=crash_after_approved_action, args=(world.url,)
        )
        process.start()
        try:
            process.join(timeout=30)
            assert process.exitcode == 74
        finally:
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
        world.store.rollback()
        assert world.store.scalar(select(func.count()).select_from(CollectionTask)) == 1
        world.store.rollback()
        with workers(world, crash="domain"):
            run = wait_run(world, run_id)
        assert run["status"] == "completed" and run["execution"] == "EXECUTED", run
        world.store.rollback()
        view = service.get(ctx, request_id)
        assert view.effective_status == "consumed" and view.execution_status == "executed"
        assert len(view.changed_ids) == 1
        assert world.store.scalar(select(func.count()).select_from(CollectionTask)) == 1
        assert world.store.scalar(select(func.count()).select_from(ActionExecution)) == 1
        assert (
            world.store.scalar(
                select(func.count())
                .select_from(AgentStep)
                .where(AgentStep.run_id == UUID(run_id), AgentStep.kind == "model")
            )
            == 1
        )
        assert world.submission_count(view.action_id) == 0


def crash_after_approved_action(url: str) -> None:
    """Actual child worker dies after domain commit, before graph checkpoint acknowledgement."""
    import asyncio
    import os

    from pydantic import SecretStr
    from pytest import MonkeyPatch

    from aco.core.settings import Settings
    from aco.features.agent.nodes import InvestigationNode
    from aco.features.agent.schemas import GraphState
    from aco.worker.app import WorkerRuntime

    original = InvestigationNode.execute

    async def crash(self: InvestigationNode, state: GraphState) -> GraphState:
        result = await original(self, state)
        if result.get("actions_saved"):
            os._exit(74)
        return result

    with MonkeyPatch.context() as patch:
        patch.setattr(InvestigationNode, "execute", crash)
        asyncio.run(WorkerRuntime(Settings(database_url=SecretStr(url))).run())


@pytest.mark.parametrize("change", ["denied", "approver_revoked", "source_changed", "expired"])
def test_current_approval_refusals_leave_no_task_or_consumption(change: str) -> None:
    from datetime import timedelta

    from fastapi import HTTPException

    from aco.features.accounting.models import InvoiceSnapshot
    from aco.features.identity.models import now

    with action_world() as world:
        response = world.client.post(
            f"/api/workspaces/{world.ctx.tenant_id}/agent-runs",
            json={
                "invoice_ids": [str(world.invoice_id)],
                "objective": "Request approval before creating an internal task",
                "request_id": str(uuid4()),
            },
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["id"]
        with workers(world, crash="domain"):
            assert wait_run(world, run_id)["status"] == "awaiting_approval"
        world.store.rollback()
        request = world.store.scalar(select(ApprovalRequest))
        assert request is not None
        request_id = request.id
        service = HumanApprovalService(world.store)
        own = service.get(world.ctx, request_id)
        command = ApprovalDecisionCommand(
            expected_version=own.version,
            reviewed_action_version=own.action_version,
            reviewed_material_hash=own.material_hash,
            reviewed_binding_hash=own.current_binding_hash,
            decision="approved",
            reason="Exact reviewed scope",
        )
        with pytest.raises(HTTPException, match="self_approval_prohibited"):
            service.decide_approval(world.ctx, request_id, command)
        world.store.rollback()
        ctx = independent_reviewer(world)
        view = service.get(ctx, request_id)
        command = command.model_copy(
            update={
                "reviewed_binding_hash": view.current_binding_hash,
                "decision": "denied" if change == "denied" else "approved",
            }
        )
        with pytest.raises(HTTPException, match="APPROVAL_REVIEW_CHANGED"):
            service.decide_approval(
                ctx, request_id, command.model_copy(update={"reviewed_binding_hash": "0" * 64})
            )
        service.decide_approval(ctx, request_id, command)
        world.store.commit()
        with pytest.raises(HTTPException, match="STALE_APPROVAL"):
            service.decide_approval(ctx, request_id, command)
        world.store.rollback()
        if change == "approver_revoked":
            member = world.store.scalar(
                select(Membership).where(
                    Membership.tenant_id == ctx.tenant_id, Membership.user_id == ctx.actor_id
                )
            )
            assert member is not None
            member.active = False
            member.version += 1
        elif change == "source_changed":
            invoice = world.store.get(InvoiceSnapshot, world.invoice_id)
            assert invoice is not None
            invoice.source_revision = "CHANGED-AFTER-REVIEW"
            invoice.version += 1
        elif change == "expired":
            request = world.store.get(ApprovalRequest, request_id)
            assert request is not None
            request.granted_at = now() - timedelta(hours=2)
            request.expires_at = now() - timedelta(hours=1)
        world.store.commit()
        with workers(world, crash="domain"):
            run = wait_run(world, run_id)
        assert run["status"] in {"completed", "failed"}, run
        assert not any(p["executed"] for p in run["result"]["proposals"])
        world.store.rollback()
        request = world.store.get(ApprovalRequest, request_id)
        assert request is not None and request.consumed_at is None
        assert world.store.scalar(select(func.count()).select_from(CollectionTask)) == 0
        assert world.store.scalar(select(func.count()).select_from(ActionExecution)) == 0
        assert world.submission_count(request.action_id) == 0
