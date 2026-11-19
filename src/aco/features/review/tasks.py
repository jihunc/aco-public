"""Human-only task CRUD against canonical CollectionTask records."""

from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.features.accounting.models import InvoiceSnapshot
from aco.features.identity.models import Membership, now
from aco.features.identity.service import IdentityService, Permission
from aco.features.review.dto import TaskCreate, TaskPage, TaskPatch, TaskView
from aco.features.review.models import CollectionTask
from aco.features.review.queue import ReviewQueue, task_view


def apply_reviewed_task_patch(row: CollectionTask, command: TaskPatch) -> None:
    """Field/CAS helper only; caller must authorize and lock the exact task and queue first."""
    if row.version != command.expected_version:
        raise HTTPException(409, "STALE_TASK")
    for field in ("title", "notes", "assignee_id", "priority", "due_at", "status"):
        if field in command.model_fields_set:
            setattr(row, field, getattr(command, field))
    row.version += 1


class HumanTaskService:
    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def _human(ctx: TenantContext) -> None:
        if not isinstance(ctx, TenantContext):
            raise HTTPException(403, "HUMAN_REQUIRED")

    def _assignee(self, ctx: TenantContext, user_id: UUID | None) -> None:
        if user_id is None:
            return
        membership = self.db.scalar(
            select(Membership).where(
                Membership.tenant_id == ctx.tenant_id,
                Membership.user_id == user_id,
                Membership.active.is_(True),
            )
        )
        if membership is None:
            raise HTTPException(403, "FOREIGN_ASSIGNEE")

    def _row(self, ctx: TenantContext, task_id: UUID, lock: bool = False) -> CollectionTask:
        query = (
            select(CollectionTask)
            .where(
                CollectionTask.tenant_id == ctx.tenant_id,
                CollectionTask.id == task_id,
                CollectionTask.deleted_at.is_(None),
            )
            .execution_options(populate_existing=True)
        )
        row = self.db.scalar(query.with_for_update() if lock else query)
        if row is None:
            raise HTTPException(404, "TASK_NOT_FOUND")
        return row

    def create(self, ctx: TenantContext, command: TaskCreate) -> TaskView:
        self._human(ctx)
        identity = IdentityService(self.db)
        identity.authorize(ctx, Permission.TASK_WRITE)
        self._assignee(ctx, command.assignee_id)
        if command.invoice_id is not None:
            invoice = self.db.scalar(
                select(InvoiceSnapshot).where(
                    InvoiceSnapshot.id == command.invoice_id, InvoiceSnapshot.tenant_id == ctx.tenant_id
                )
            )
            if invoice is None:
                raise HTTPException(403, "FOREIGN_INVOICE")
        queue = ReviewQueue(self.db)
        state = queue.state(ctx.tenant_id)
        current = queue.rows(ctx.tenant_id)
        rank = max((row.priority_rank for row in current), default=Decimal(0)) + 1
        row = CollectionTask(
            tenant_id=ctx.tenant_id,
            source_action_id=None,
            source_event_id=None,
            invoice_id=command.invoice_id,
            creator_id=ctx.actor_id,
            assignee_id=command.assignee_id,
            title=command.title,
            notes=command.notes,
            priority=command.priority,
            priority_rank=rank,
            due_at=command.due_at,
            status="open",
            command={},
        )
        self.db.add(row)
        state.version += 1
        self.db.flush()
        identity.audit(ctx, "task.created", row.id)
        return task_view(row)

    def get(self, ctx: TenantContext, task_id: UUID) -> TaskView:
        self._human(ctx)
        IdentityService(self.db).authorize(ctx, Permission.INVOICE_READ)
        return task_view(self._row(ctx, task_id))

    def list(self, ctx: TenantContext, invoice_id: UUID | None = None) -> TaskPage:
        self._human(ctx)
        IdentityService(self.db).authorize(ctx, Permission.INVOICE_READ)
        query = select(CollectionTask).where(
            CollectionTask.tenant_id == ctx.tenant_id, CollectionTask.deleted_at.is_(None)
        )
        if invoice_id is not None:
            query = query.where(CollectionTask.invoice_id == invoice_id)
        rows = list(
            self.db.scalars(
                query.order_by(CollectionTask.priority_rank, CollectionTask.created_at, CollectionTask.id)
            )
        )
        return TaskPage(items=[task_view(r) for r in rows])

    def update(self, ctx: TenantContext, task_id: UUID, command: TaskPatch) -> TaskView:
        self._human(ctx)
        identity = IdentityService(self.db)
        identity.authorize(ctx, Permission.TASK_WRITE)
        state = ReviewQueue(self.db).state(ctx.tenant_id)
        row = self._row(ctx, task_id, lock=True)
        if row.version != command.expected_version:
            raise HTTPException(409, "STALE_TASK")
        if "assignee_id" in command.model_fields_set:
            self._assignee(ctx, command.assignee_id)
        apply_reviewed_task_patch(row, command)
        state.version += 1
        self.db.flush()
        identity.audit(ctx, "task.updated", row.id)
        return task_view(row)

    def soft_delete(self, ctx: TenantContext, task_id: UUID, expected_version: int) -> TaskView:
        self._human(ctx)
        identity = IdentityService(self.db)
        identity.authorize(ctx, Permission.TASK_WRITE)
        state = ReviewQueue(self.db).state(ctx.tenant_id)
        row = self._row(ctx, task_id, lock=True)
        if row.version != expected_version:
            raise HTTPException(409, "STALE_TASK")
        row.deleted_at = now()
        row.version += 1
        state.version += 1
        self.db.flush()
        identity.audit(ctx, "task.deleted", row.id)
        return task_view(row)
