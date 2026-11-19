"""Read persisted queue/error/dead-letter evidence under current Membership."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.features.actions.models import ActionExecution, ScheduledAction
from aco.features.identity.api import Database, Subject
from aco.features.identity.service import IdentityService, Permission

router = APIRouter(prefix="/api/workspaces/{workspace_id}/scheduled-actions")


class ExecutionView(BaseModel):
    attempt: int
    status: str
    started_at: datetime
    finished_at: datetime | None
    retry_delay_seconds: int | None
    provider_operation_id: str | None


class ActionView(BaseModel):
    id: UUID
    kind: str
    invoice_id: UUID
    version: int
    status: str
    attempts: int
    due_at: datetime
    last_error: str | None
    automatic_resend: bool
    operation_key: str
    job_id: int | None
    executions: list[ExecutionView]


def view(store: Session, action: ScheduledAction) -> ActionView:
    executions = store.scalars(
        select(ActionExecution)
        .where(ActionExecution.action_id == action.id, ActionExecution.tenant_id == action.tenant_id)
        .order_by(ActionExecution.attempt)
    ).all()
    return ActionView(
        id=action.id,
        kind=action.kind,
        invoice_id=action.invoice_id,
        version=action.version,
        status=action.status,
        attempts=action.attempts,
        due_at=action.due_at,
        last_error=action.last_error,
        automatic_resend=action.automatic_resend,
        operation_key=action.operation_key,
        job_id=action.job_id,
        executions=[ExecutionView.model_validate(row, from_attributes=True) for row in executions],
    )


@router.get("", response_model=list[ActionView])
async def list_actions(
    workspace_id: UUID,
    subject: Subject,
    db: Database,
    kind: Literal["policy_due", "accounting_recheck", "internal_task", "approval_request", "agent_email"]
    | None = None,
    status: Literal[
        "queued",
        "executing",
        "awaiting_approval",
        "succeeded",
        "failed",
        "unknown_outcome",
        "cancelled",
        "dead_letter",
    ]
    | None = None,
) -> list[ActionView]:
    def read(store: Session) -> list[ActionView]:
        identity = IdentityService(store)
        ctx = identity.resolve_context(subject, workspace_id)
        identity.authorize(ctx, Permission.INVOICE_READ)
        query = select(ScheduledAction).where(ScheduledAction.tenant_id == ctx.tenant_id)
        if kind is not None:
            query = query.where(ScheduledAction.kind == kind)
        if status is not None:
            query = query.where(ScheduledAction.status == status)
        actions = store.scalars(
            query.order_by(ScheduledAction.created_at.desc(), ScheduledAction.id.desc()).limit(100)
        ).all()
        return [view(store, action) for action in actions]

    return await db.run_sync(read)


@router.get("/{action_id}", response_model=ActionView)
async def detail(workspace_id: UUID, action_id: UUID, subject: Subject, db: Database) -> ActionView:
    def read(store: Session) -> ActionView:
        identity = IdentityService(store)
        ctx = identity.resolve_context(subject, workspace_id)
        identity.authorize(ctx, Permission.INVOICE_READ)
        action = store.scalar(
            select(ScheduledAction).where(
                ScheduledAction.id == action_id, ScheduledAction.tenant_id == ctx.tenant_id
            )
        )
        if action is None:
            raise HTTPException(404, "ACTION_NOT_FOUND")
        return view(store, action)

    return await db.run_sync(read)
