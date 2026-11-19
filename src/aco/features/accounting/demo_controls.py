"""Explicit synthetic-workspace generation fence; never erase source or effect history."""

import json
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import AwareDatetime, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.core.types import ActionResult, ActionStatus, StrictModel
from aco.features.accounting.models import DemoReset, DemoSourceRecord, InvoiceSnapshot
from aco.features.actions.models import ActionExecution, ScheduledAction
from aco.features.actions.service import ActionLease, ActionService, worker_context
from aco.features.identity.models import Actor, ProviderConnection
from aco.features.identity.service import IdentityService, Permission
from aco.features.usage.budgets import BudgetService
from aco.features.usage.dto import UsageOutcome
from aco.features.usage.models import BudgetReservation, ProviderUsageEvent, UsageAttempt


class DemoResetCommand(StrictModel):
    request_id: UUID
    expected_generation: int = Field(ge=1)
    evidence: str = Field(min_length=10, max_length=1000)


class ResetResult(StrictModel):
    request_id: UUID
    previous_generation: int
    generation: int
    outbound_paused: Literal[True] = True
    automation_enabled: Literal[False] = False
    cancelled_action_ids: tuple[UUID, ...]
    unknown_action_ids: tuple[UUID, ...]
    preserved_uncertain_action_ids: tuple[UUID, ...]
    cancelled_run_ids: tuple[UUID, ...]
    released_reservation_ids: tuple[UUID, ...]
    source_restoration: Literal["PENDING", "RESTORED"] = "PENDING"
    restored_invoice_ids: tuple[UUID, ...] = ()
    restored_customer_ids: tuple[UUID, ...] = ()
    reversal_allocation_ids: tuple[UUID, ...] = ()
    source_event_ids: tuple[UUID, ...] = ()


def _unused_reservation(db: Session, action: ScheduledAction) -> BudgetReservation | None:
    """Only an attached, never-invoked exact operation can prove an unused grant."""
    if action.reservation_id is None or action.attempts:
        return None
    reservation = db.scalar(
        select(BudgetReservation)
        .where(BudgetReservation.id == action.reservation_id, BudgetReservation.tenant_id == action.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        reservation is None
        or reservation.status != "RESERVED"
        or reservation.real_provider_calls
        or reservation.spent_amount
        or reservation.operation_key != action.operation_key
        or reservation.provider_connection_id != action.provider_connection_id
        or db.scalar(select(ActionExecution.id).where(ActionExecution.action_id == action.id).limit(1))
        or db.scalar(
            select(ProviderUsageEvent.id)
            .where(
                ProviderUsageEvent.tenant_id == action.tenant_id,
                ProviderUsageEvent.provider_connection_id == action.provider_connection_id,
                ProviderUsageEvent.operation_key == action.operation_key,
            )
            .limit(1)
        )
        or db.scalar(
            select(UsageAttempt.id)
            .where(
                UsageAttempt.tenant_id == action.tenant_id,
                UsageAttempt.provider_connection_id == action.provider_connection_id,
                UsageAttempt.correlation_id == action.correlation_id,
            )
            .limit(1)
        )
    ):
        return None
    return reservation


def reset_demo_workspace(
    db: Session, ctx: TenantContext, command: DemoResetCommand, *, app_env: str, _persist: bool = True
) -> ResetResult:
    if not isinstance(ctx, TenantContext):
        raise HTTPException(403, "HUMAN_CONTEXT_REQUIRED")
    identity = IdentityService(db)
    tenant = identity.authorize(ctx, Permission.DEMO_RESET)
    if app_env not in {"local", "demo"} or not tenant.is_demo:
        raise HTTPException(403, "DEMO_ONLY")
    # Existing tenant-first NO KEY UPDATE lock serializes replay and every fresh
    # worker/provider authority gate. No shared clock or permission is synthesized.
    prior = db.scalar(
        select(DemoReset).where(DemoReset.tenant_id == tenant.id, DemoReset.request_id == command.request_id)
    )
    if prior is not None:
        if prior.actor_id != ctx.actor_id or prior.command != command.model_dump(mode="json"):
            raise HTTPException(409, "DEMO_RESET_REQUEST_CONFLICT")
        return ResetResult.model_validate_json(json.dumps(prior.result))
    if tenant.version != command.expected_generation:
        raise HTTPException(409, "DEMO_GENERATION_CONFLICT")
    invoices = list(db.scalars(select(InvoiceSnapshot).where(InvoiceSnapshot.tenant_id == tenant.id)))
    accounting = list(
        db.scalars(
            select(ProviderConnection).where(
                ProviderConnection.tenant_id == tenant.id, ProviderConnection.provider_type == "accounting"
            )
        )
    )
    if not invoices or not accounting:
        raise HTTPException(409, "DEMO_SOURCE_NOT_SEEDED")
    if any(not invoice.synthetic for invoice in invoices) or any(
        connection.provider != "demo" for connection in accounting
    ):
        raise HTTPException(403, "SYNTHETIC_ACCOUNTING_REQUIRED")
    actions = list(
        db.scalars(
            select(ScheduledAction)
            .where(ScheduledAction.tenant_id == tenant.id)
            .order_by(ScheduledAction.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    cancelled, unknown, preserved, released = [], [], [], []
    service = ActionService(db)
    for action in actions:
        if action.status == "executing":
            if action.lease_token is None:
                raise HTTPException(409, "DEMO_RESET_EXECUTION_INCONSISTENT")
            service.complete_action(
                worker_context(action),
                ActionLease(
                    action_id=action.id,
                    version=action.version,
                    token=action.lease_token,
                    attempt=action.attempts,
                ),
                ActionResult(
                    action_id=action.id,
                    status=ActionStatus.UNKNOWN_OUTCOME,
                    error_code="DEMO_RESET_IN_FLIGHT",
                ),
            )
            unknown.append(action.id)
        elif action.status == "unknown_outcome":
            preserved.append(action.id)
        elif action.status in {"queued", "awaiting_approval"}:
            unused = _unused_reservation(db, action)
            service.cancel_action(ctx, action.id, action.version)
            cancelled.append(action.id)
            if unused is not None:
                BudgetService(db).reconcile_reservation(
                    ctx,
                    unused.id,
                    UsageOutcome(
                        key=f"demo-reset:{command.request_id}", status="NO_EFFECT", verified_unused=True
                    ),
                )
                unused.automatic_retry = False
                released.append(unused.id)
    from aco.features.agent.models import AgentRun
    from aco.features.agent.service import AgentService

    cancelled_runs = []
    for run in db.scalars(
        select(AgentRun)
        .where(
            AgentRun.tenant_id == tenant.id, AgentRun.status.in_(("queued", "running", "awaiting_approval"))
        )
        .order_by(AgentRun.id)
        .with_for_update()
    ):
        AgentService(db).cancel(ctx, run.id, run.version)
        cancelled_runs.append(run.id)
    for actor in db.scalars(
        select(Actor).where(Actor.tenant_id == tenant.id, Actor.kind == "service").with_for_update()
    ):
        actor.version += 1
    previous = tenant.version
    tenant.outbound_paused = True
    tenant.automation_enabled = False
    tenant.version += 1
    result = ResetResult(
        request_id=command.request_id,
        previous_generation=previous,
        generation=tenant.version,
        cancelled_action_ids=tuple(cancelled),
        unknown_action_ids=tuple(unknown),
        preserved_uncertain_action_ids=tuple(preserved),
        cancelled_run_ids=tuple(cancelled_runs),
        released_reservation_ids=tuple(released),
    )
    if _persist:
        record_reset(db, ctx, command, result)
    db.flush()
    return result


def record_reset(db: Session, ctx: TenantContext, command: DemoResetCommand, result: ResetResult) -> None:
    audit = IdentityService(db).audit(
        ctx,
        "demo.reset",
        command.request_id,
        {"command": command.model_dump_json(), "result": result.model_dump_json()},
    )
    db.add(
        DemoReset(
            tenant_id=ctx.tenant_id,
            actor_id=ctx.actor_id,
            request_id=command.request_id,
            audit_id=audit.id,
            previous_generation=result.previous_generation,
            generation=result.generation,
            command=command.model_dump(mode="json"),
            result=result.model_dump(mode="json"),
        )
    )
    db.flush()


async def reset_and_restore_workspace(
    db: AsyncSession, ctx: TenantContext, command: DemoResetCommand, *, app_env: str
) -> ResetResult:
    from aco.features.accounting.demo_restoration import restore_seed_source

    result = await db.run_sync(
        lambda store: reset_demo_workspace(store, ctx, command, app_env=app_env, _persist=False)
    )
    replay = await db.scalar(
        select(DemoReset.id).where(
            DemoReset.tenant_id == ctx.tenant_id, DemoReset.request_id == command.request_id
        )
    )
    if replay is not None:
        return result
    restored = await restore_seed_source(db, ctx, command.request_id)
    result = result.model_copy(
        update={
            "source_restoration": "RESTORED",
            "restored_invoice_ids": restored.invoice_ids,
            "restored_customer_ids": restored.customer_ids,
            "reversal_allocation_ids": restored.reversal_allocation_ids,
            "source_event_ids": restored.source_event_ids,
        }
    )
    await db.run_sync(lambda store: record_reset(store, ctx, command, result))
    return result


class ResetState(StrictModel):
    generation: int
    outbound_paused: bool
    automation_enabled: bool
    can_reset: bool
    eligibility_reasons: tuple[str, ...]
    last_reset: ResetResult | None
    last_reset_at: AwareDatetime | None


def read_demo_reset(db: Session, ctx: TenantContext, *, app_env: str) -> ResetState:
    if not isinstance(ctx, TenantContext):
        raise HTTPException(403, "HUMAN_CONTEXT_REQUIRED")
    tenant = IdentityService(db).authorize(ctx, Permission.DEMO_RESET)
    reasons = []
    if app_env not in {"local", "demo"}:
        reasons.append("DEMO_ENVIRONMENT_REQUIRED")
    if not tenant.is_demo:
        reasons.append("DEMO_WORKSPACE_REQUIRED")
    invoices = list(db.scalars(select(InvoiceSnapshot).where(InvoiceSnapshot.tenant_id == tenant.id)))
    connections = list(
        db.scalars(
            select(ProviderConnection).where(
                ProviderConnection.tenant_id == tenant.id, ProviderConnection.provider_type == "accounting"
            )
        )
    )
    if not invoices or not connections:
        reasons.append("DEMO_SOURCE_NOT_SEEDED")
    if any(not row.synthetic for row in invoices) or any(row.provider != "demo" for row in connections):
        reasons.append("SYNTHETIC_ACCOUNTING_REQUIRED")
    if any(row.status != "READY" for row in connections):
        reasons.append("DEMO_SOURCE_UNAVAILABLE")
    markers = list(
        db.scalars(
            select(DemoSourceRecord).where(
                DemoSourceRecord.tenant_id == tenant.id,
                DemoSourceRecord.kind == "scenario_input",
                DemoSourceRecord.external_id == "future-stories",
                DemoSourceRecord.revision == 1,
            )
        )
    )
    if len(markers) != 1 or markers[0].payload.get("synthetic") is not True:
        reasons.append("CANONICAL_SEED_REQUIRED")
    last = db.scalar(
        select(DemoReset)
        .where(DemoReset.tenant_id == tenant.id)
        .order_by(DemoReset.created_at.desc(), DemoReset.id)
        .limit(1)
    )
    return ResetState(
        generation=tenant.version,
        outbound_paused=tenant.outbound_paused,
        automation_enabled=tenant.automation_enabled,
        can_reset=not reasons,
        eligibility_reasons=tuple(reasons),
        last_reset=ResetResult.model_validate_json(json.dumps(last.result)) if last else None,
        last_reset_at=last.created_at if last else None,
    )
