"""Actual SDK/worker internal human work, with current guards and proposal replay."""

from dataclasses import replace
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from action_support import action_world
from evaluations.test_collection_agent import proposal, run_sdk
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.actions.models import ActionExecution, ScheduledAction
from aco.features.agent.context import RunAuthority
from aco.features.agent.models import AgentRun, AgentStep
from aco.features.agent.nodes import InvestigationNode
from aco.features.agent.schemas import GraphState
from aco.features.communications.models import Communication
from aco.features.identity.models import AuditEvent, Membership, now
from aco.features.review.models import CollectionTask
from aco.features.tools.gateway import ExecutedAction, execute_internal
from aco.features.tools.internal_followup import RequestHumanReview


@pytest.mark.parametrize(
    "kind", ["request_human_review", "schedule_followup", "verify_accounting", "follow_up"]
)
def test_actual_internal_work_persists_assigned_due_task_and_replays_once(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    original = InvestigationNode.execute
    repetitions = 0

    async def replay(self: InvestigationNode, state: GraphState) -> GraphState:
        nonlocal repetitions
        if kind == "request_human_review":
            async with self.sessions.begin() as db:
                reads = list(
                    await db.scalars(
                        select(AgentStep).where(
                            AgentStep.run_id == self.authority.run_id,
                            AgentStep.kind == "read",
                            AgentStep.status == "completed",
                        )
                    )
                )
                invoice_id = reads[0].invoice_id
                assert invoice_id is not None
                request = RequestHumanReview(
                    invoice_id=invoice_id, title="Refusal probe", evidence_step_ids=[r.id for r in reads]
                )
                for authority, command, expected in (
                    (replace(self.authority, tenant_id=uuid4()), request, "RUN_AUTHORITY_DENIED"),
                    (replace(self.authority, actor_id=uuid4()), request, "RUN_AUTHORITY_REVOKED"),
                    (replace(self.authority, lease_token=uuid4()), request, "RUN_AUTHORITY_REVOKED"),
                    (
                        self.authority,
                        request.model_copy(update={"invoice_id": uuid4()}),
                        "INVOICE_OUTSIDE_RUN_SCOPE",
                    ),
                    (
                        self.authority,
                        request.model_copy(update={"evidence_step_ids": [uuid4(), uuid4()]}),
                        "ACTION_EVIDENCE_DENIED",
                    ),
                ):
                    with pytest.raises(HTTPException) as refused:

                        def attempt(
                            store: Session,
                            authority: RunAuthority = authority,
                            command: RequestHumanReview = command,
                        ) -> ExecutedAction:
                            return execute_internal(
                                store, authority, command, 999, command.invoice_id, command.evidence_step_ids
                            )

                        await db.run_sync(attempt)
                    assert refused.value.status_code == 403 and refused.value.detail == expected
        first = await original(self, state)
        second = await original(self, state)
        repetitions += 1
        assert first == second
        return second

    monkeypatch.setattr(InvestigationNode, "execute", replay)
    with action_world() as world:
        invoice = world.store.get(InvoiceSnapshot, world.invoice_id)
        assert invoice is not None
        due = now() + timedelta(days=2)
        proposed = proposal(invoice, kind, title="Human verification of source evidence")
        if kind == "schedule_followup":
            proposed["followup_at"] = due.isoformat()
        saved = run_sdk(world, [invoice], [proposed])
        assert saved["status"] == "completed", saved
        assert saved["execution"] == "EXECUTED" and repetitions == 1
        result = saved["result"]["proposals"][0]
        assert result["executed"] is True and len(result["changed_ids"]) == 1
        world.store.rollback()
        task = world.store.get(CollectionTask, UUID(result["changed_ids"][0]))
        assert task is not None
        action = world.store.get(ScheduledAction, task.source_action_id)
        run = world.store.get(AgentRun, UUID(saved["id"]))
        assert action is not None and run is not None
        assert action.kind == "agent_internal" and action.status == "succeeded"
        expected_kind = "schedule_followup" if kind == "schedule_followup" else "request_human_review"
        assert action.command["kind"] == expected_kind
        assert task.assignee_id == run.initiator_id == world.ctx.actor_id
        assert task.creator_id == run.worker_actor_id and task.creator_id != task.assignee_id
        assert task.invoice_id == invoice.id and task.source_agent_run_id == run.id
        assert task.source_action_id == UUID(result["action_id"])
        assert task.due_at == (due if kind == "schedule_followup" else None)
        payload = action.command["payload"]
        assert isinstance(payload, dict)
        evidence_ids = payload["evidence_step_ids"]
        assert evidence_ids and task.command["evidence_step_ids"] == evidence_ids
        step = world.store.get(AgentStep, action.agent_step_id)
        assert step is not None and step.status == "completed" and step.tool_name == expected_kind
        audit = world.store.scalar(select(AuditEvent).where(AuditEvent.action == "agent." + expected_kind))
        assert audit is not None and audit.target_id == task.id
        assert audit.details["evidence_step_ids"] == evidence_ids
        assert audit.details["effect"] == (
            "scheduled_human_task" if kind == "schedule_followup" else "human_review_task"
        )
        assert world.store.scalar(select(func.count()).select_from(CollectionTask)) == 1
        assert world.store.scalar(select(func.count()).select_from(ActionExecution)) == 1
        assert world.store.scalar(select(func.count()).select_from(ScheduledAction)) == 1
        assert world.store.scalar(select(func.count()).select_from(Communication)) == 0
        task_id = task.id
        world.store.rollback()
        detail = world.client.get(f"/api/workspaces/{world.ctx.tenant_id}/tasks/{task_id}")
        assert detail.status_code == 200
        assert detail.json()["assignee_id"] == str(world.ctx.actor_id)
        if kind == "schedule_followup":
            assert datetime.fromisoformat(detail.json()["due_at"].replace("Z", "+00:00")) == due
        elif kind == "follow_up":
            assert result["kind"] == "request_human_review", "Undated work must not claim scheduled followup"
            assert detail.json()["due_at"] is None


@pytest.mark.parametrize("refusal", ["past_due", "unobserved_case", "revoked_membership"])
def test_internal_work_refuses_stale_time_case_or_current_authority(
    monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    original = InvestigationNode.execute
    with action_world() as world:
        invoice = world.store.get(InvoiceSnapshot, world.invoice_id)
        assert invoice is not None
        command = proposal(invoice, "request_human_review", title="Current human review")
        if refusal == "past_due":
            command = proposal(
                invoice, "schedule_followup", followup_at=(now() - timedelta(seconds=1)).isoformat()
            )
        elif refusal == "unobserved_case":
            command["case_binding"] = {"case_id": str(uuid4()), "case_version": 1}
        else:

            async def revoke(self: InvestigationNode, state: GraphState) -> GraphState:
                world.store.rollback()
                member = world.store.scalar(
                    select(Membership).where(
                        Membership.tenant_id == world.ctx.tenant_id,
                        Membership.user_id == world.ctx.actor_id,
                    )
                )
                assert member is not None
                member.active = False
                member.version += 1
                world.store.commit()
                return await original(self, state)

            monkeypatch.setattr(InvestigationNode, "execute", revoke)
        saved = run_sdk(world, [invoice], [command])
        assert saved["status"] == "failed", saved
        assert saved["error_code"] == {
            "past_due": "FOLLOWUP_TIME_NOT_FUTURE",
            "unobserved_case": "TASK_CASE_CHANGED",
            "revoked_membership": "RUN_AUTHORITY_REVOKED",
        }[refusal]
        world.store.rollback()
        assert world.store.scalar(select(func.count()).select_from(CollectionTask)) == 0
        assert world.store.scalar(select(func.count()).select_from(ActionExecution)) == 0
        assert world.store.scalar(select(func.count()).select_from(ScheduledAction)) == 0
        assert world.store.scalar(select(func.count()).select_from(Communication)) == 0
