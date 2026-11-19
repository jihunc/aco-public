"""Human tasks use canonical rows, committed audit, and tenant queue versions."""

from datetime import timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import HTTPException
from identity_support import actor_client
from sqlalchemy import select

from aco.core.context import TenantContext
from aco.core.types import Role
from aco.features.identity.models import AuditEvent, Membership, User, now
from aco.features.review.dto import QueueReorder, TaskCreate, TaskPatch
from aco.features.review.models import CollectionTask
from aco.features.review.queue import ReviewQueue
from aco.features.review.tasks import HumanTaskService


def context(tenant: Any, user: Any, role: Any = "owner") -> Any:
    return TenantContext(tenant_id=tenant, actor_id=user.id, role=Role(role), correlation_id=uuid4())


def test_task_crud_soft_delete_preserves_audit() -> None:
    with actor_client("owner") as (_, store, user, tenant):
        service = HumanTaskService(store)
        ctx = context(tenant, user)
        created = service.create(ctx, TaskCreate(title="Confirm purchase order", priority=2))
        store.commit()
        row = store.get(CollectionTask, created.id)
        assert row and row.source_action_id is None and row.source_event_id is None
        assert row.creator_id == user.id and row.invoice_id is None
        updated = service.update(
            ctx, created.id, TaskPatch(title="Confirm PO with buyer", expected_version=created.version)
        )
        store.commit()
        assert updated.version > created.version and updated.title == "Confirm PO with buyer"
        with pytest.raises(HTTPException) as stale:
            service.update(ctx, created.id, TaskPatch(notes="stale", expected_version=created.version))
        assert stale.value.status_code == 409
        deleted = service.soft_delete(ctx, created.id, updated.version)
        store.commit()
        retained = store.get(CollectionTask, created.id)
        assert deleted.deleted_at and retained is not None and retained.deleted_at
        assert {
            a.action for a in store.scalars(select(AuditEvent).where(AuditEvent.target_id == created.id))
        } >= {"task.created", "task.updated", "task.deleted"}
        assert service.list(ctx).items == []


def test_reorder_persists_api_order_without_financial_change() -> None:
    with actor_client("owner") as (_, store, user, tenant):
        ctx = context(tenant, user)
        tasks = HumanTaskService(store)
        a = tasks.create(ctx, TaskCreate(title="First"))
        b = tasks.create(ctx, TaskCreate(title="Second"))
        c = tasks.create(ctx, TaskCreate(title="Third"))
        store.commit()
        queue = ReviewQueue(store).get(ctx)
        updated = ReviewQueue(store).reorder(
            ctx, QueueReorder(ids=[c.id, a.id, b.id], expected_version=queue.version)
        )
        store.commit()
        store.expire_all()
        assert [item.id for item in ReviewQueue(store).get(ctx).items] == [c.id, a.id, b.id]
        assert updated.version == queue.version + 1
        with pytest.raises(HTTPException) as stale:
            ReviewQueue(store).reorder(
                ctx, QueueReorder(ids=[a.id, b.id, c.id], expected_version=queue.version)
            )
        assert stale.value.status_code == 409
        with pytest.raises(HTTPException) as missing:
            ReviewQueue(store).reorder(
                ctx, QueueReorder(ids=[a.id, b.id, uuid4()], expected_version=updated.version)
            )
        assert missing.value.status_code == 422


def test_task_authority_and_assignee_are_current_tenant_members() -> None:
    with actor_client("owner") as (_, store, user, tenant):
        owner = context(tenant, user)
        task = HumanTaskService(store).create(owner, TaskCreate(title="Escalate query"))
        store.commit()
        viewer = User(
            issuer="synthetic", subject=str(uuid4()), email="viewer@example.test", email_verified=True
        )
        store.add(viewer)
        store.flush()
        membership = Membership(tenant_id=tenant, user_id=viewer.id, role="viewer")
        store.add(membership)
        store.commit()
        viewer_ctx = context(tenant, viewer, "viewer")
        assert HumanTaskService(store).get(viewer_ctx, task.id).id == task.id
        with pytest.raises(HTTPException) as forbidden:
            HumanTaskService(store).create(viewer_ctx, TaskCreate(title="No"))
        assert forbidden.value.status_code == 403
        with pytest.raises(HTTPException) as outsider:
            HumanTaskService(store).update(
                owner, task.id, TaskPatch(assignee_id=uuid4(), expected_version=task.version)
            )
        assert outsider.value.status_code == 403
        membership.active = False
        store.commit()
        with pytest.raises(HTTPException) as revoked:
            HumanTaskService(store).get(viewer_ctx, task.id)
        assert revoked.value.status_code == 403


def test_http_task_crud_reorder_csrf_and_reload() -> None:
    with actor_client("owner") as (client, store, user, tenant):
        path = f"/api/workspaces/{tenant}"
        created = client.post(
            path + "/tasks",
            json={
                "title": "Check contract",
                "priority": 2,
                "assignee_id": str(user.id),
                "due_at": (now() + timedelta(days=1)).isoformat(),
            },
        )
        assert created.status_code == 201, created.text
        task = created.json()
        assert task["origin"] == "human" and task["creator_id"] == str(user.id)
        second = client.post(path + "/tasks", json={"title": "Call accounts"})
        assert second.status_code == 201, second.text
        second_id = second.json()["id"]
        queue = client.get(path + "/review-queue")
        assert queue.status_code == 200 and [t["id"] for t in queue.json()["items"]] == [
            task["id"],
            second_id,
        ]
        reordered = client.patch(
            path + "/review-queue",
            json={"ids": [second_id, task["id"]], "expected_version": queue.json()["version"]},
        )
        assert reordered.status_code == 200, reordered.text
        assert [t["id"] for t in client.get(path + "/review-queue").json()["items"]] == [
            second_id,
            task["id"],
        ]
        no_csrf = client.post(
            path + "/tasks", json={"title": "Forbidden"}, headers={"x-csrf-token": "invalid"}
        )
        assert no_csrf.status_code == 403
        current_version = next(t["version"] for t in reordered.json()["items"] if t["id"] == task["id"])
        patched = client.patch(
            path + "/tasks/" + task["id"],
            json={"title": "Check signed contract", "expected_version": current_version},
        )
        assert patched.status_code == 200, patched.text
        assert client.get(path + "/tasks/" + task["id"]).json()["title"] == "Check signed contract"
        deleted = client.request(
            "DELETE", path + "/tasks/" + task["id"], json={"expected_version": patched.json()["version"]}
        )
        assert deleted.status_code == 200, deleted.text
        assert client.get(path + "/tasks/" + task["id"]).status_code == 404


def test_task_assignees_expose_only_active_current_members() -> None:
    with actor_client("operator") as (client, store, user, tenant):
        response = client.get(f"/api/workspaces/{tenant}/task-assignees")
        assert response.status_code == 200
        assert response.json() == [{"user_id": str(user.id), "email": user.email}]


@pytest.mark.parametrize("field", ["title", "notes", "priority", "status"])
def test_http_patch_rejects_null_required_fields_without_mutation(field: Any) -> None:
    with actor_client("owner") as (client, store, _, tenant):
        path = f"/api/workspaces/{tenant}/tasks"
        task = client.post(path, json={"title": "Retain this"}).json()
        response = client.patch(
            f"{path}/{task['id']}", json={"expected_version": task["version"], field: None}
        )
        assert response.status_code == 422, response.text
        current = client.get(f"{path}/{task['id']}").json()
        assert current == task


def test_real_worker_context_cannot_enter_human_task_or_queue_routes() -> None:
    from action_support import action_world, enqueue

    from aco.features.actions.service import worker_context

    with action_world() as world:
        ref = enqueue(world)
        action = world.action(ref.action_id)
        worker = worker_context(action)
        with pytest.raises(HTTPException) as create_denied:
            HumanTaskService(world.store).create(
                cast(Any, worker), TaskCreate(title="Worker must not mint human task")
            )
        assert create_denied.value.status_code == 403
        with pytest.raises(HTTPException) as queue_denied:
            ReviewQueue(world.store).get(cast(Any, worker))
        assert queue_denied.value.status_code == 403
        assert (
            world.store.scalar(select(CollectionTask).where(CollectionTask.tenant_id == world.ctx.tenant_id))
            is None
        )


def test_human_edit_and_soft_delete_generated_task_preserve_source_links() -> None:
    from action_support import action_world, enqueue

    from aco.features.review.internal_work import InternalTaskCommand, InternalWorkService

    with action_world() as world:
        ref = enqueue(world)
        command = InternalTaskCommand(
            source_action_id=ref.action_id,
            source_event_id=world.source_event_id,
            invoice_id=world.invoice_id,
            title="Source review",
        )
        task = InternalWorkService(world.store).apply_internal_task(world.ctx, command)
        world.store.commit()
        edited = HumanTaskService(world.store).update(
            world.ctx, task.task_id, TaskPatch(title="Customer follow-up", expected_version=task.version)
        )
        assert edited.title == "Customer follow-up" and edited.source_action_id == ref.action_id
        deleted = HumanTaskService(world.store).soft_delete(world.ctx, task.task_id, edited.version)
        assert deleted.deleted_at
        world.store.commit()
        current = world.store.get(CollectionTask, task.task_id)
        assert current and current.command["title"] == "Source review"
        assert current.source_action_id == ref.action_id and current.source_event_id == world.source_event_id
        assert current.invoice_id == world.invoice_id and current.creator_id == world.ctx.actor_id
        assert current.deleted_at is not None
        assert {
            a.action
            for a in world.store.scalars(select(AuditEvent).where(AuditEvent.target_id == task.task_id))
        } >= {"task.updated", "task.deleted"}


def test_due_deadline_stays_ahead_of_manual_reorder() -> None:
    with actor_client("owner") as (_, store, user, tenant):
        ctx = context(tenant, user)
        tasks = HumanTaskService(store)
        ordinary = tasks.create(ctx, TaskCreate(title="Ordinary"))
        due = tasks.create(ctx, TaskCreate(title="Already due", due_at=now() - timedelta(hours=1)))
        store.commit()
        queue = ReviewQueue(store).get(ctx)
        assert [r.id for r in queue.items] == [due.id, ordinary.id]
        reordered = ReviewQueue(store).reorder(
            ctx, QueueReorder(ids=[ordinary.id, due.id], expected_version=queue.version)
        )
        store.commit()
        assert [r.id for r in reordered.items] == [due.id, ordinary.id]


def test_task_edit_invalidates_previously_read_queue_version() -> None:
    with actor_client("owner") as (_, store, user, tenant):
        ctx = context(tenant, user)
        task = HumanTaskService(store).create(ctx, TaskCreate(title="Before edit"))
        store.commit()
        before = ReviewQueue(store).get(ctx)
        HumanTaskService(store).update(
            ctx, task.id, TaskPatch(title="After edit", expected_version=task.version)
        )
        store.commit()
        with pytest.raises(HTTPException) as stale:
            ReviewQueue(store).reorder(ctx, QueueReorder(ids=[task.id], expected_version=before.version))
        assert stale.value.status_code == 409
