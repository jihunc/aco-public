"""Selected, observed task updates. No new assignment, deadline, financial or outreach authority."""

import json
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import AwareDatetime, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.types import Role, StrictModel
from aco.features.accounting.models import InvoiceSnapshot
from aco.features.agent.context import RunAuthority, current_run, selected_invoices
from aco.features.agent.models import AgentStep
from aco.features.identity.models import AuditEvent, Membership
from aco.features.identity.service import ROLE_PERMISSIONS, Permission
from aco.features.review.dto import TaskPatch, TaskStatus
from aco.features.review.models import CollectionTask
from aco.features.review.queue import ReviewQueue, task_view
from aco.features.review.tasks import apply_reviewed_task_patch
from aco.features.tools.authorization import AgentActionContext, require_agent_action


class TaskUpdate(StrictModel):
    task_id: UUID
    expected_version: int = Field(ge=1)
    title: str | None = Field(
        default=None, min_length=1, max_length=240, exclude_if=lambda value: value is None
    )
    notes: str | None = Field(default=None, max_length=4000, exclude_if=lambda value: value is None)
    status: TaskStatus | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("title")
    @classmethod
    def nonblank_title(cls, value: str | None) -> str | None:
        return TaskPatch.title_nonblank(value)

    @model_validator(mode="after")
    def nonempty(self) -> "TaskUpdate":
        if self.title is None and self.notes is None and self.status is None:
            raise ValueError("Select a task title, notes or status change")
        return self

    def patch(self) -> TaskPatch:
        fields = {
            field: getattr(self, field)
            for field in ("title", "notes", "status")
            if getattr(self, field) is not None
        }
        return TaskPatch.model_validate({"expected_version": self.expected_version, **fields})


class UpdateCollectionTask(TaskUpdate):
    invoice_id: UUID
    evidence_step_ids: list[UUID] = Field(min_length=2, max_length=8)


class TaskFact(StrictModel):
    kind: Literal["collection_task"] = "collection_task"
    task_id: UUID
    invoice_id: UUID
    version: int
    title: str
    notes: str
    status: TaskStatus
    priority: int
    priority_rank: str
    assignee_id: UUID | None
    due_at: AwareDatetime | None
    deadline_locked: bool
    source_action_id: UUID | None
    creator_id: UUID
    origin: Literal["human", "generated"]


def task_fact(row: CollectionTask) -> TaskFact:
    view = task_view(row)
    assert view.invoice_id is not None
    return TaskFact(
        task_id=view.id,
        invoice_id=view.invoice_id,
        version=view.version,
        title=view.title,
        notes=view.notes,
        status=view.status,
        priority=view.priority,
        priority_rank=view.priority_rank,
        assignee_id=view.assignee_id,
        due_at=view.due_at,
        deadline_locked=view.deadline_locked,
        source_action_id=view.source_action_id,
        creator_id=view.creator_id,
        origin=view.origin,
    )


def task_facts(db: Session, invoice: InvoiceSnapshot) -> list[TaskFact]:
    return [
        task_fact(row)
        for row in db.scalars(
            select(CollectionTask)
            .where(
                CollectionTask.tenant_id == invoice.tenant_id,
                CollectionTask.invoice_id == invoice.id,
                CollectionTask.deleted_at.is_(None),
            )
            .order_by(CollectionTask.created_at, CollectionTask.id)
            .limit(51)
            .execution_options(populate_existing=True)
        )
    ]


def validate_task_update(
    db: Session, authority: RunAuthority, command: UpdateCollectionTask, *, lock: bool = False
) -> tuple[CollectionTask, UUID]:
    from aco.features.tools.schemas import ReadEvidence

    run = current_run(db, authority)
    if command.invoice_id not in selected_invoices(db, run):
        raise HTTPException(403, "INVOICE_OUTSIDE_RUN_SCOPE")
    member = db.scalar(
        select(Membership)
        .where(Membership.tenant_id == run.tenant_id, Membership.user_id == run.initiator_id)
        .execution_options(populate_existing=True)
    )
    if (
        member is None
        or not member.active
        or Permission.TASK_WRITE not in ROLE_PERMISSIONS[Role(member.role)]
    ):
        raise HTTPException(403, "TASK_WRITE_DENIED")
    query = (
        select(CollectionTask)
        .where(
            CollectionTask.id == command.task_id,
            CollectionTask.tenant_id == run.tenant_id,
            CollectionTask.invoice_id == command.invoice_id,
            CollectionTask.deleted_at.is_(None),
        )
        .execution_options(populate_existing=True)
    )
    row = db.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise HTTPException(403, "TASK_OUTSIDE_RUN_SCOPE")
    if row.version != command.expected_version:
        raise HTTPException(409, "STALE_TASK")
    steps = db.scalars(
        select(AgentStep).where(
            AgentStep.id.in_(command.evidence_step_ids),
            AgentStep.tenant_id == run.tenant_id,
            AgentStep.run_id == run.id,
            AgentStep.invoice_id == command.invoice_id,
            AgentStep.tool_name == "list_collection_tasks",
            AgentStep.status == "completed",
            AgentStep.kind == "read",
        )
    )
    for step in steps:
        if step.output is None:
            continue
        evidence = ReadEvidence.model_validate_json(json.dumps(step.output))
        if evidence.step_id != step.id or evidence.invoice_id != command.invoice_id or not evidence.available:
            continue
        for fact in evidence.records:
            if isinstance(fact, TaskFact) and fact.task_id == row.id:
                # Deadline lock is evaluated at read time; no deadline/rank edit is permitted here.
                if fact.model_dump(exclude={"deadline_locked"}) != task_fact(row).model_dump(
                    exclude={"deadline_locked"}
                ):
                    raise HTTPException(409, "TASK_READ_CHANGED")
                return row, step.id
    raise HTTPException(403, "TASK_READ_EVIDENCE_REQUIRED")


def update_collection_task(db: Session, ctx: AgentActionContext, command: UpdateCollectionTask) -> list[UUID]:
    if not isinstance(ctx, AgentActionContext):
        raise HTTPException(403, "AGENT_ACTION_REQUIRED")
    action = require_agent_action(db, ctx, "update_collection_task", command)
    if action.invoice_id != command.invoice_id:
        raise HTTPException(403, "COMMAND_INVOICE_CONFLICT")
    state = ReviewQueue(db).state(ctx.tenant_id)
    row, task_read_step_id = validate_task_update(db, ctx.authority, command, lock=True)
    before = task_fact(row).model_dump(mode="json")
    apply_reviewed_task_patch(row, command.patch())
    state.version += 1
    db.flush()
    db.add(
        AuditEvent(
            tenant_id=ctx.tenant_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.correlation_id,
            action="task.updated_by_agent",
            target_id=row.id,
            details={
                "run_id": str(ctx.authority.run_id),
                "action_id": str(action.id),
                "step_id": str(ctx.step_id),
                "task_read_step_id": str(task_read_step_id),
                "evidence_step_ids": [str(key) for key in command.evidence_step_ids],
                "before": before,
                "after": task_fact(row).model_dump(mode="json"),
                "effect": "internal_task_update_only",
            },
        )
    )
    return [row.id]
