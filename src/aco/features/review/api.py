"""Authenticated human task and review queue routes."""

from uuid import UUID

from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.features.identity.api import Database, Subject
from aco.features.identity.models import Membership, User
from aco.features.identity.service import IdentityService, Permission
from aco.features.review.dto import (
    QueueReorder,
    QueueView,
    TaskAssignee,
    TaskCreate,
    TaskDelete,
    TaskPage,
    TaskPatch,
    TaskView,
)
from aco.features.review.queue import ReviewQueue
from aco.features.review.tasks import HumanTaskService

router = APIRouter(prefix="/api/workspaces/{workspace_id}")


@router.get("/task-assignees", response_model=list[TaskAssignee])
async def assignees(workspace_id: UUID, subject: Subject, db: Database) -> list[TaskAssignee]:
    def read(store: Session) -> list[TaskAssignee]:
        identity = IdentityService(store)
        ctx = identity.resolve_context(subject, workspace_id)
        identity.authorize(ctx, Permission.TASK_WRITE)
        rows = store.execute(
            select(Membership, User)
            .join(User, User.id == Membership.user_id)
            .where(Membership.tenant_id == ctx.tenant_id, Membership.active.is_(True))
            .order_by(User.email)
        ).all()
        return [TaskAssignee(user_id=user.id, email=user.email) for _, user in rows]

    return await db.run_sync(read)


@router.get("/tasks", response_model=TaskPage)
async def list_tasks(
    workspace_id: UUID, subject: Subject, db: Database, invoice_id: UUID | None = None
) -> TaskPage:
    return await db.run_sync(
        lambda store: HumanTaskService(store).list(
            IdentityService(store).resolve_context(subject, workspace_id), invoice_id
        )
    )


@router.post("/tasks", status_code=201, response_model=TaskView)
async def create_task(workspace_id: UUID, command: TaskCreate, subject: Subject, db: Database) -> TaskView:
    return await db.run_sync(
        lambda store: HumanTaskService(store).create(
            IdentityService(store).resolve_context(subject, workspace_id), command
        )
    )


@router.get("/tasks/{task_id}", response_model=TaskView)
async def get_task(workspace_id: UUID, task_id: UUID, subject: Subject, db: Database) -> TaskView:
    return await db.run_sync(
        lambda store: HumanTaskService(store).get(
            IdentityService(store).resolve_context(subject, workspace_id), task_id
        )
    )


@router.patch("/tasks/{task_id}", response_model=TaskView)
async def patch_task(
    workspace_id: UUID, task_id: UUID, command: TaskPatch, subject: Subject, db: Database
) -> TaskView:
    return await db.run_sync(
        lambda store: HumanTaskService(store).update(
            IdentityService(store).resolve_context(subject, workspace_id), task_id, command
        )
    )


@router.delete("/tasks/{task_id}", response_model=TaskView)
async def delete_task(
    workspace_id: UUID, task_id: UUID, command: TaskDelete, subject: Subject, db: Database
) -> TaskView:
    return await db.run_sync(
        lambda store: HumanTaskService(store).soft_delete(
            IdentityService(store).resolve_context(subject, workspace_id), task_id, command.expected_version
        )
    )


@router.get("/review-queue", response_model=QueueView)
async def queue(workspace_id: UUID, subject: Subject, db: Database) -> QueueView:
    return await db.run_sync(
        lambda store: ReviewQueue(store).get(IdentityService(store).resolve_context(subject, workspace_id))
    )


@router.patch("/review-queue", response_model=QueueView)
async def reorder(workspace_id: UUID, command: QueueReorder, subject: Subject, db: Database) -> QueueView:
    return await db.run_sync(
        lambda store: ReviewQueue(store).reorder(
            IdentityService(store).resolve_context(subject, workspace_id), command
        )
    )
