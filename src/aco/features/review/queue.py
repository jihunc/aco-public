"""Tenant-scoped, versioned task ordering; due deadlines remain first."""

from decimal import Decimal
from typing import cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.features.identity.models import now
from aco.features.identity.service import IdentityService, Permission
from aco.features.review.dto import QueueReorder, QueueView, TaskStatus, TaskView
from aco.features.review.models import CollectionTask, ReviewQueueState


def task_view(row: CollectionTask) -> TaskView:
    return TaskView(
        id=row.id,
        invoice_id=row.invoice_id,
        source_action_id=row.source_action_id,
        creator_id=row.creator_id,
        assignee_id=row.assignee_id,
        title=row.title,
        notes=row.notes,
        status=cast(TaskStatus, row.status),
        priority=row.priority,
        priority_rank=f"{row.priority_rank:.10f}",
        due_at=row.due_at,
        deleted_at=row.deleted_at,
        version=row.version,
        origin="generated" if row.source_action_id else "human",
        deadline_locked=bool(row.due_at and row.due_at <= now() and row.status != "done"),
    )


class ReviewQueue:
    def __init__(self, db: Session):
        self.db = db

    def state(self, tenant_id: UUID) -> ReviewQueueState:
        self.db.execute(
            insert(ReviewQueueState)
            .values(tenant_id=tenant_id, version=1)
            .on_conflict_do_nothing(index_elements=[ReviewQueueState.tenant_id])
        )
        state = self.db.scalar(
            select(ReviewQueueState)
            .where(ReviewQueueState.tenant_id == tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        assert state is not None
        return state

    def rows(self, tenant_id: UUID, lock: bool = False) -> list[CollectionTask]:
        query = (
            select(CollectionTask)
            .where(
                CollectionTask.tenant_id == tenant_id,
                CollectionTask.deleted_at.is_(None),
                CollectionTask.status != "done",
            )
            .order_by(CollectionTask.priority_rank, CollectionTask.created_at, CollectionTask.id)
            .execution_options(populate_existing=True)
        )
        rows = list(self.db.scalars(query.with_for_update() if lock else query))
        locked = [r for r in rows if r.due_at and r.due_at <= now()]
        locked.sort(key=lambda r: (r.due_at, r.priority_rank, r.id))
        return locked + [r for r in rows if r not in locked]

    def get(self, ctx: TenantContext) -> QueueView:
        if not isinstance(ctx, TenantContext):
            raise HTTPException(403, "HUMAN_REQUIRED")
        IdentityService(self.db).authorize(ctx, Permission.INVOICE_READ)
        state = self.db.scalar(select(ReviewQueueState).where(ReviewQueueState.tenant_id == ctx.tenant_id))
        return QueueView(
            version=state.version if state else 1, items=[task_view(r) for r in self.rows(ctx.tenant_id)]
        )

    def reorder(self, ctx: TenantContext, command: QueueReorder) -> QueueView:
        if not isinstance(ctx, TenantContext):
            raise HTTPException(403, "HUMAN_REQUIRED")
        identity = IdentityService(self.db)
        identity.authorize(ctx, Permission.TASK_WRITE)
        state = self.state(ctx.tenant_id)
        if state.version != command.expected_version:
            raise HTTPException(409, "STALE_QUEUE")
        rows = self.rows(ctx.tenant_id, lock=True)
        if set(command.ids) != {r.id for r in rows} or len(command.ids) != len(rows):
            raise HTTPException(422, "QUEUE_MEMBERSHIP_CHANGED")
        locked = [r for r in rows if r.due_at and r.due_at <= now()]
        unlocked_by_id = {r.id: r for r in rows if r not in locked}
        ordered = locked + [unlocked_by_id[id] for id in command.ids if id in unlocked_by_id]
        for rank, row in enumerate(ordered, 1):
            row.priority_rank = Decimal(rank)
            row.version += 1
        state.version += 1
        self.db.flush()
        identity.audit(ctx, "task_queue.reordered", state.id)
        return QueueView(version=state.version, items=[task_view(r) for r in ordered])
