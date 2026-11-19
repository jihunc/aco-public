"""Actual selected task reads and guarded updates through the maintained SDK worker."""

from datetime import timedelta
from uuid import UUID

import pytest
from action_support import action_world
from evaluations.test_collection_agent import proposal, run_sdk
from sqlalchemy import func, select

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.actions.models import ScheduledAction
from aco.features.agent.models import AgentStep
from aco.features.agent.nodes import InvestigationNode
from aco.features.agent.schemas import GraphState
from aco.features.cases.models import CollectionCase
from aco.features.communications.models import Communication
from aco.features.identity.models import AuditEvent, Membership, now
from aco.features.review.models import CollectionTask


@pytest.mark.parametrize("status", ["in_progress", "done"])
def test_actual_task_read_update_persists_without_changing_urgency_or_finances(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    original_execute = InvestigationNode.execute

    async def replay(self: InvestigationNode, state: GraphState) -> GraphState:
        first = await original_execute(self, state)
        assert await original_execute(self, state) == first
        return first

    monkeypatch.setattr(InvestigationNode, "execute", replay)
    with action_world() as world:
        invoice = world.store.get(InvoiceSnapshot, world.invoice_id)
        assert invoice is not None
        due = now() - timedelta(hours=1)
        created = world.client.post(
            f"/api/workspaces/{world.ctx.tenant_id}/tasks",
            json={
                "invoice_id": str(invoice.id),
                "title": "Verify synthetic source manually",
                "notes": "Original human task, no source mutation.",
                "priority": 3,
                "due_at": due.isoformat(),
                "assignee_id": str(world.ctx.actor_id),
            },
        )
        assert created.status_code == 201, created.text
        before = created.json()
        saved = run_sdk(
            world,
            [invoice],
            [
                proposal(
                    invoice,
                    "update_collection_task",
                    task_update={
                        "task_id": before["id"],
                        "expected_version": before["version"],
                        "title": "Source review recorded; human follow-up remains",
                        "notes": (
                            "Reviewed existing source evidence without claiming payment or case resolution."
                        ),
                        "status": status,
                    },
                )
            ],
            extra_reads=[("list_collection_tasks", invoice.id)],
        )
        assert saved["status"] == "completed", saved
        result = saved["result"]["proposals"][0]
        assert result["executed"] and result["changed_ids"] == [before["id"]]
        detail = world.client.get(f"/api/workspaces/{world.ctx.tenant_id}/tasks/{before['id']}")
        assert detail.status_code == 200
        updated = detail.json()
        assert updated["version"] == before["version"] + 1
        assert updated["title"] == "Source review recorded; human follow-up remains"
        assert updated["status"] == status
        assert updated["deadline_locked"] is (status != "done")
        for field in (
            "invoice_id",
            "source_action_id",
            "creator_id",
            "assignee_id",
            "priority",
            "priority_rank",
            "due_at",
            "deleted_at",
            "origin",
        ):
            assert updated[field] == before[field], field
        world.store.rollback()
        assert world.store.scalar(select(func.count()).select_from(CollectionTask)) == 1
        assert world.store.scalar(select(func.count()).select_from(Communication)) == 0
        assert world.store.scalar(select(func.count()).select_from(CollectionCase)) == 0
        action = world.store.get(ScheduledAction, UUID(result["action_id"]))
        assert action is not None and action.status == "succeeded"
        assert action.command["kind"] == "update_collection_task"
        read = world.store.scalar(
            select(AgentStep).where(
                AgentStep.run_id == UUID(saved["id"]), AgentStep.tool_name == "list_collection_tasks"
            )
        )
        assert read is not None and read.status == "completed" and read.output is not None
        records = read.output["records"]
        assert isinstance(records, list) and records[0]["task_id"] == before["id"]
        assert records[0]["version"] == before["version"]
        audit = world.store.scalar(select(AuditEvent).where(AuditEvent.action == "task.updated_by_agent"))
        assert audit is not None and audit.target_id == UUID(before["id"])
        assert audit.details["task_read_step_id"] == str(read.id)


@pytest.mark.parametrize("refusal", ["stale", "deleted", "unselected", "foreign", "missing_read", "revoked"])
def test_agent_task_update_refuses_changed_scope_or_current_authority(
    monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    original_execute = InvestigationNode.execute
    with action_world() as world:
        invoice = world.store.get(InvoiceSnapshot, world.invoice_id)
        assert invoice is not None
        target_invoice_id = invoice.id
        target_tenant_id = world.ctx.tenant_id
        if refusal == "unselected":
            other = world.store.scalar(select(InvoiceSnapshot).where(InvoiceSnapshot.id != invoice.id))
            assert other is not None
            target_invoice_id = other.id
        elif refusal == "foreign":
            workspace = world.client.post(
                "/api/workspaces", json={"name": "Other task company", "timezone": "UTC"}
            )
            assert workspace.status_code == 201
            target_tenant_id = UUID(workspace.json()["id"])
            seeded = world.client.post(f"/api/workspaces/{target_tenant_id}/demo/accounting/seed", json={})
            assert seeded.status_code == 201
            world.store.rollback()
            other = world.store.scalar(
                select(InvoiceSnapshot).where(InvoiceSnapshot.tenant_id == target_tenant_id)
            )
            assert other is not None
            target_invoice_id = other.id
        created = world.client.post(
            f"/api/workspaces/{target_tenant_id}/tasks",
            json={"invoice_id": str(target_invoice_id), "title": "Retained task", "notes": "Original notes"},
        )
        assert created.status_code == 201
        before = created.json()

        async def change_before_effect(self: InvestigationNode, state: GraphState) -> GraphState:
            world.store.rollback()
            if refusal == "stale":
                changed = world.client.patch(
                    f"/api/workspaces/{target_tenant_id}/tasks/{before['id']}",
                    json={"expected_version": before["version"], "title": "Current human edit"},
                )
                assert changed.status_code == 200
            elif refusal == "deleted":
                deleted = world.client.request(
                    "DELETE",
                    f"/api/workspaces/{target_tenant_id}/tasks/{before['id']}",
                    json={"expected_version": before["version"]},
                )
                assert deleted.status_code == 200
            elif refusal == "revoked":
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
            return await original_execute(self, state)

        monkeypatch.setattr(InvestigationNode, "execute", change_before_effect)
        saved = run_sdk(
            world,
            [invoice],
            [
                proposal(
                    invoice,
                    "update_collection_task",
                    task_update={
                        "task_id": before["id"],
                        "expected_version": before["version"],
                        "notes": "Unpermitted update",
                    },
                )
            ],
            extra_reads=[] if refusal == "missing_read" else [("list_collection_tasks", invoice.id)],
        )
        assert saved["status"] == "failed", saved
        assert (
            saved["error_code"]
            == {
                "stale": "STALE_TASK",
                "deleted": "TASK_OUTSIDE_RUN_SCOPE",
                "unselected": "TASK_OUTSIDE_RUN_SCOPE",
                "foreign": "TASK_OUTSIDE_RUN_SCOPE",
                "missing_read": "TASK_READ_EVIDENCE_REQUIRED",
                "revoked": "RUN_AUTHORITY_REVOKED",
            }[refusal]
        )
        world.store.rollback()
        task = world.store.get(CollectionTask, UUID(before["id"]))
        assert task is not None and task.notes == "Original notes"
        assert world.store.scalar(select(func.count()).select_from(ScheduledAction)) == 0
        assert (
            world.store.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "task.updated_by_agent")
            )
            == 0
        )
