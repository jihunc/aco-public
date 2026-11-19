"""Next persisted pending action, not a fresh eligibility or delivery decision."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aco.core.types import StrictModel
from aco.features.actions.models import ScheduledAction


class NextSavedAction(StrictModel):
    availability: Literal["saved_pending", "none_scheduled"] = "none_scheduled"
    id: UUID | None = None
    kind: str | None = None
    status: str | None = None
    channel: str | None = None
    due_at: datetime | None = None
    eligibility: Literal["not_evaluated"] = "not_evaluated"


async def next_saved_actions(
    db: AsyncSession, tenant_id: UUID, invoice_ids: list[UUID]
) -> dict[UUID, NextSavedAction]:
    if not invoice_ids:
        return {}
    ranked = (
        select(
            ScheduledAction.id,
            func.row_number()
            .over(
                partition_by=ScheduledAction.invoice_id,
                order_by=(ScheduledAction.due_at, ScheduledAction.id),
            )
            .label("rank"),
        )
        .where(
            ScheduledAction.tenant_id == tenant_id,
            ScheduledAction.invoice_id.in_(invoice_ids),
            ScheduledAction.status.in_(("proposed", "validated", "awaiting_approval", "queued", "executing")),
        )
        .subquery()
    )
    rows = await db.scalars(
        select(ScheduledAction)
        .join(ranked, ScheduledAction.id == ranked.c.id)
        .where(
            ScheduledAction.tenant_id == tenant_id,
            ranked.c.rank == 1,
        )
    )
    result: dict[UUID, NextSavedAction] = {}
    for row in rows:
        channel = "unknown"
        if row.kind == "policy_due":
            saved = row.command.get("channel")
            if saved in ("email", "voice"):
                channel = str(saved)
        elif row.kind == "agent_email":
            channel = "email"
        elif row.kind in {"voice_session", "voice_internal"}:
            channel = "voice"
        elif row.kind in {
            "accounting_recheck",
            "internal_task",
            "approval_request",
            "agent_internal",
            "promise_check",
        }:
            channel = "internal"
        result[row.invoice_id] = NextSavedAction(
            availability="saved_pending",
            id=row.id,
            kind=row.kind,
            status=row.status,
            channel=channel,
            due_at=row.due_at,
        )
    return result
