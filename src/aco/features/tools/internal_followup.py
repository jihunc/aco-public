"""Guarded human work only: no provider dispatch, source refresh or agent recursion."""

from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException
from pydantic import AwareDatetime, Field, field_validator
from sqlalchemy.orm import Session

from aco.core.types import StrictModel
from aco.features.agent.context import RunAuthority, current_run, selected_invoices
from aco.features.cases.task_binding import TaskCaseBinding, current_task_case
from aco.features.identity.models import AuditEvent, now
from aco.features.review.models import CollectionTask
from aco.features.review.queue import ReviewQueue
from aco.features.tools.authorization import AgentActionContext, require_agent_action


class RequestHumanReview(StrictModel):
    invoice_id: UUID
    title: str = Field(min_length=1, max_length=240)
    notes: str = Field(default="", max_length=4000)
    evidence_step_ids: list[UUID] = Field(min_length=2, max_length=8)
    case_binding: TaskCaseBinding | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("title")
    @classmethod
    def nonblank_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a human work title")
        return value


class ScheduleFollowup(RequestHumanReview):
    due_at: AwareDatetime


def internal_work_kind(command: RequestHumanReview | ScheduleFollowup) -> str:
    return "schedule_followup" if isinstance(command, ScheduleFollowup) else "request_human_review"


def validate_internal_work(
    db: Session, authority: RunAuthority, command: RequestHumanReview | ScheduleFollowup
) -> None:
    run = current_run(db, authority)
    if command.invoice_id not in selected_invoices(db, run):
        raise HTTPException(403, "INVOICE_OUTSIDE_RUN_SCOPE")
    if isinstance(command, ScheduleFollowup) and command.due_at <= now():
        raise HTTPException(409, "FOLLOWUP_TIME_NOT_FUTURE")
    if command.case_binding is not None:
        current_task_case(db, run, command.invoice_id, command.case_binding, command.evidence_step_ids)


def apply_internal_work(
    db: Session, ctx: AgentActionContext, command: RequestHumanReview | ScheduleFollowup
) -> list[UUID]:
    kind = internal_work_kind(command)
    action = require_agent_action(db, ctx, kind, command)
    if action.invoice_id != command.invoice_id:
        raise HTTPException(403, "COMMAND_INVOICE_CONFLICT")
    validate_internal_work(db, ctx.authority, command)
    run = current_run(db, ctx.authority)
    queue = ReviewQueue(db)
    state = queue.state(ctx.tenant_id)
    rows = queue.rows(ctx.tenant_id)
    task = CollectionTask(
        tenant_id=ctx.tenant_id,
        source_action_id=action.id,
        source_event_id=None,
        source_agent_run_id=run.id,
        invoice_id=command.invoice_id,
        creator_id=ctx.actor_id,
        assignee_id=run.initiator_id,
        title=command.title,
        notes=command.notes,
        due_at=command.due_at if isinstance(command, ScheduleFollowup) else None,
        command=command.model_dump(mode="json"),
        priority_rank=max((row.priority_rank for row in rows), default=Decimal(0)) + 1,
    )
    db.add(task)
    state.version += 1
    db.flush()
    db.add(
        AuditEvent(
            tenant_id=ctx.tenant_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.correlation_id,
            action="agent." + kind,
            target_id=task.id,
            details={
                "run_id": str(run.id),
                "action_id": str(action.id),
                "step_id": str(ctx.step_id),
                "source_communication_id": str(run.source_communication_id)
                if run.source_communication_id
                else None,
                "evidence_step_ids": [str(key) for key in command.evidence_step_ids],
                "assignee_id": str(run.initiator_id),
                "effect": "scheduled_human_task"
                if isinstance(command, ScheduleFollowup)
                else "human_review_task",
            },
        )
    )
    return [task.id]
