"""Reviewed text replacement preserves history and requires a fresh independent grant."""

from uuid import UUID, uuid4

import pytest
from action_support import action_world, workers
from fastapi import FastAPI
from identity_support import add_actor
from integration.test_agent_approval import wait_run
from integration.test_approval_scope import request
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError

from aco.features.identity.models import Membership
from aco.features.review.models import ApprovalDecision, ApprovalRequest, CollectionTask


def captured(view: dict[str, object]) -> dict[str, object]:
    return {
        "expected_version": view["version"],
        "reviewed_action_version": view["action_version"],
        "reviewed_material_hash": view["material_hash"],
        "reviewed_binding_hash": view["current_binding_hash"],
    }


def test_content_replacement_preserves_original_and_decisions_and_rejects_stale_capture() -> None:
    with action_world() as world:
        old_id = request(world)
        app = world.client.app
        assert isinstance(app, FastAPI)
        user, editor = add_actor(world.store, app, "editor@synthetic.invalid")
        world.store.add(Membership(tenant_id=world.ctx.tenant_id, user_id=user.id, role="finance_manager"))
        world.store.commit()
        path = f"/api/workspaces/{world.ctx.tenant_id}/approvals/{old_id}"
        view = editor.get(path).json()
        approved = editor.post(
            path + "/decisions",
            json={**captured(view), "decision": "approved", "reason": "Original text reviewed"},
        )
        assert approved.status_code == 200, approved.text
        view = editor.get(path).json()
        command = {
            **captured(view),
            "edit": {"kind": "outbound_content", "content": "Human reviewed replacement content"},
            "reason": "Clarify the wording without changing scope",
        }
        response = editor.post(path + "/modifications", json=command)
        assert response.status_code == 200, response.text
        new = response.json()
        assert new["id"] != str(old_id) and new["action_id"] != view["action_id"]
        assert new["action_version"] == view["action_version"] + 1
        assert new["supersedes_id"] == str(old_id) and new["editor_id"] == str(user.id)
        assert new["status"] == "pending" and not new["can_approve"]
        assert "self_approval_prohibited" in new["reasons"]
        assert new["proposal"]["content"] == "Human reviewed replacement content"
        for key in (
            "destination",
            "invoice_ids",
            "evidence_ids",
            "policy_version_id",
            "cost_bound",
            "consent_mode",
        ):
            assert new["proposal"][key] == view["proposal"][key]
        old = editor.get(path).json()
        assert not old["approval_valid"] and not old["can_modify"]
        assert old["replacement_id"] == new["id"]
        assert old["proposal"] == view["proposal"] and old["decisions"] == view["decisions"]
        assert editor.post(path + "/modifications", json=command).status_code == 409
        new_path = f"/api/workspaces/{world.ctx.tenant_id}/approvals/{new['id']}"
        assert (
            editor.post(
                new_path + "/decisions",
                json={**captured(new), "decision": "approved", "reason": "Cannot approve my material edit"},
            ).status_code
            == 403
        )
        assert world.submission_count(UUID(new["action_id"])) == 0
        world.store.rollback()
        assert len(list(world.store.scalars(select(ApprovalDecision)))) == 1
        for field, value in (
            ("editor_id", world.ctx.actor_id),
            ("edit_reason", "Rewrite history"),
            ("supersedes_id", None),
        ):
            with pytest.raises(DBAPIError), world.store.begin_nested():
                world.store.execute(
                    update(ApprovalRequest)
                    .where(ApprovalRequest.id == UUID(new["id"]))
                    .values({field: value})
                )
        world.store.rollback()
        second = world.client.get(new_path).json()
        changed = world.client.post(
            new_path + "/modifications",
            json={
                **captured(second),
                "edit": {"kind": "outbound_content", "content": "Second independent human correction"},
                "reason": "Additional precise wording",
            },
        )
        assert changed.status_code == 200, changed.text
        latest = changed.json()
        # The former material editor is still excluded after another human edits.
        viewed_by_first_editor = editor.get(
            f"/api/workspaces/{world.ctx.tenant_id}/approvals/{latest['id']}"
        ).json()
        assert not viewed_by_first_editor["can_approve"]
        assert "self_approval_prohibited" in viewed_by_first_editor["reasons"]


def test_actual_agent_resume_executes_edited_title_and_notes_after_independent_review() -> None:
    from integration.test_case_task_approval import prepare_case

    from aco.features.cases.models import CaseHistory

    with action_world() as world:
        case_id, case_version = prepare_case(world)
        response = world.client.post(
            f"/api/workspaces/{world.ctx.tenant_id}/agent-runs",
            json={
                "invoice_ids": [str(world.invoice_id)],
                "objective": (
                    f"Request approval before creating an internal task for case {case_id} "
                    f"version {case_version}"
                ),
                "request_id": str(uuid4()),
            },
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["id"]
        with workers(world, crash="domain"):
            assert wait_run(world, run_id)["status"] == "awaiting_approval"
        world.store.rollback()
        original = world.store.scalar(select(ApprovalRequest))
        assert original is not None
        original_id, original_proposal = original.id, original.proposal
        world.store.rollback()
        path = f"/api/workspaces/{world.ctx.tenant_id}/approvals/{original_id}"
        before = world.client.get(path).json()
        edit = {
            **captured(before),
            "edit": {
                "kind": "internal_task",
                "title": "Human revised exact task",
                "notes": "Review original source documents; no financial change.",
            },
            "reason": "Clarify the concrete internal work",
        }
        response = world.client.post(path + "/modifications", json=edit)
        assert response.status_code == 200, response.text
        updated = response.json()
        assert not updated["can_approve"] and updated["can_modify"]
        history = world.store.scalar(
            select(CaseHistory).where(
                CaseHistory.case_id == case_id, CaseHistory.event == "case.approval_modified"
            )
        )
        assert history is not None and history.substantive_human and history.actor_id == world.ctx.actor_id
        assert history.details["approval_id"] == updated["id"]
        world.store.rollback()
        app = world.client.app
        assert isinstance(app, FastAPI)
        user, reviewer = add_actor(world.store, app, "fresh-reviewer@synthetic.invalid")
        world.store.add(Membership(tenant_id=world.ctx.tenant_id, user_id=user.id, role="finance_manager"))
        world.store.commit()
        new_path = f"/api/workspaces/{world.ctx.tenant_id}/approvals/{updated['id']}"
        reviewed = reviewer.get(new_path).json()
        assert reviewed["can_approve"], reviewed
        decision = reviewer.post(
            new_path + "/decisions",
            json={
                **captured(reviewed),
                "decision": "approved",
                "reason": "Independently reviewed modified task",
            },
        )
        assert decision.status_code == 200, decision.text
        with workers(world, crash="domain"):
            assert wait_run(world, run_id)["status"] == "completed"
        world.store.rollback()
        tasks = list(
            world.store.scalars(
                select(CollectionTask).where(CollectionTask.source_agent_run_id == UUID(run_id))
            )
        )
        assert len(tasks) == 1
        assert tasks[0].title == "Human revised exact task"
        assert tasks[0].notes == "Review original source documents; no financial change."
        assert tasks[0].source_action_id == UUID(updated["action_id"])
        saved = world.store.get(ApprovalRequest, original_id)
        assert saved is not None and saved.proposal == original_proposal and saved.consumed_at is None
        world.store.rollback()
        final = reviewer.get(new_path).json()
        assert final["effective_status"] == "consumed" and not final["can_modify"]


def test_modification_api_preserves_scope_csrf_current_roles_and_review_binding() -> None:
    with action_world() as world:
        old_id = request(world)
        app = world.client.app
        assert isinstance(app, FastAPI)
        user, editor = add_actor(world.store, app, "scope-editor@synthetic.invalid")
        membership = Membership(tenant_id=world.ctx.tenant_id, user_id=user.id, role="finance_manager")
        world.store.add(membership)
        world.store.commit()
        path = f"/api/workspaces/{world.ctx.tenant_id}/approvals/{old_id}"
        before = editor.get(path).json()
        command = {
            **captured(before),
            "edit": {"kind": "outbound_content", "content": "Replacement content"},
            "reason": "Precise human correction",
        }
        assert (
            editor.post(
                path + "/modifications", json=command, headers={"x-csrf-token": "invalid"}
            ).status_code
            == 403
        )
        for role in ("viewer", "operator"):
            membership.role = role
            world.store.commit()
            assert not editor.get(path).json()["can_modify"]
            assert editor.post(path + "/modifications", json=command).status_code == 403
        membership.role = "finance_manager"
        world.store.commit()
        for key in (
            "expected_version",
            "reviewed_action_version",
            "reviewed_material_hash",
            "reviewed_binding_hash",
        ):
            altered = {**command, key: 999 if key.endswith("version") else "a" * 64}
            assert editor.post(path + "/modifications", json=altered).status_code == 409
        assert (
            editor.post(
                path + "/modifications",
                json={
                    **command,
                    "edit": {
                        "kind": "outbound_content",
                        "content": "x",
                        "destination": "other@synthetic.invalid",
                    },
                },
            ).status_code
            == 422
        )
        assert (
            editor.post(
                path + "/modifications",
                json={**command, "edit": {"kind": "internal_task", "title": "wrong kind", "notes": ""}},
            ).status_code
            == 422
        )
        foreign_path = f"/api/workspaces/{uuid4()}/approvals/{old_id}/modifications"
        assert editor.post(foreign_path, json=command).status_code == 403
        assert editor.get(path).json()["proposal"] == before["proposal"]
        response = editor.post(path + "/modifications", json=command)
        assert response.status_code == 200, response.text
        new = response.json()
        membership.active = False
        membership.version += 1
        world.store.commit()
        current = world.client.get(f"/api/workspaces/{world.ctx.tenant_id}/approvals/{new['id']}").json()
        assert "APPROVAL_EDITOR_PERMISSION_CHANGED" in current["reasons"]
        assert not current["can_approve"] and not current["can_modify"]
