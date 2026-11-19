"""One durable attempt, with committed claim before provider invocation and fenced completion."""

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from aco.features.communications.outbound import ReminderDispatcher
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aco.core.context import ServiceContext
from aco.core.types import ActionResult, ActionStatus, ProviderErrorCode, ProviderFailure
from aco.features.actions.models import ScheduledAction
from aco.features.actions.service import ActionFailure, ActionService, worker_context
from aco.features.identity.models import now
from aco.features.identity.service import IdentityService, Permission
from aco.integrations.ports.accounting import AccountingFailure, AccountingRejectionEvidence
from aco.integrations.ports.email import EmailFailure


class ActionExecutor(Protocol):
    async def execute(
        self, ctx: ServiceContext, action: ScheduledAction, db: AsyncSession
    ) -> ActionResult: ...


class DomainExecutor:
    def __init__(self, reminders: "ReminderDispatcher | None" = None):
        self.reminders = reminders

    async def execute(self, ctx: ServiceContext, action: ScheduledAction, db: AsyncSession) -> ActionResult:
        if action.kind == "policy_due":
            from aco.features.policies.guards import execute_policy_due

            return await execute_policy_due(db, ctx, action)
        if action.kind in {"accounting_recheck", "promise_check"}:
            from aco.features.accounting.ingest import AccountingService
            from aco.features.accounting.models import InvoiceSnapshot
            from aco.integrations.demo_accounting.adapter import DemoAccountingAdapter

            invoice = await db.get(InvoiceSnapshot, action.invoice_id)
            assert invoice
            service = AccountingService(db, DemoAccountingAdapter(db, action.provider_connection_id))
            # Re-read through the canonical adapter/usage/financial UoW. No hint or
            # fabricated native event is needed for authoritative read recovery.
            await service.recheck_invoice(ctx, invoice.external_invoice_id)
            return ActionResult(action_id=action.id, status=ActionStatus.SUCCEEDED, changed_ids=(invoice.id,))
        import json

        from aco.features.review.internal_work import (
            ApprovalProposal,
            InternalTaskCommand,
            InternalWorkService,
        )

        payload = json.dumps(action.command["payload"])
        if action.kind == "internal_task":
            task = await db.run_sync(
                lambda store: InternalWorkService(store).apply_internal_task(
                    ctx, InternalTaskCommand.model_validate_json(payload)
                )
            )
            changed = task.task_id
        elif action.kind == "approval_request":
            request = await db.run_sync(
                lambda store: InternalWorkService(store).request_approval(
                    ctx, ApprovalProposal.model_validate_json(payload)
                )
            )
            changed = request.request_id
        else:
            raise HTTPException(403, "UNSUPPORTED_ACTION")
        return ActionResult(action_id=action.id, status=ActionStatus.SUCCEEDED, changed_ids=(changed,))


async def _record_promise_read_failure(
    db: AsyncSession,
    ctx: ServiceContext,
    code: ProviderErrorCode,
    *,
    rejection_evidence: AccountingRejectionEvidence | None = None,
) -> None:
    action = await db.get(ScheduledAction, ctx.action_id)
    if action is None or action.kind != "promise_check":
        return
    from aco.features.promises.performance import record_promise_source_failure

    try:
        await db.run_sync(
            lambda store: record_promise_source_failure(
                store, ctx, action.invoice_id, code.value, rejection_evidence=rejection_evidence
            )
        )
    except HTTPException as error:
        if error.status_code not in {403, 409}:
            raise
        # Revoked/superseded authority cannot append promise evidence. The canonical
        # attempt still records its failure below without asserting source success.


async def execute_action(
    sessions: async_sessionmaker[AsyncSession],
    action_id: UUID,
    expected_version: int,
    executor: ActionExecutor,
    *,
    job_id: int | None = None,
    at: datetime | None = None,
    clock: Callable[[], datetime] = now,
) -> None:
    instant = at or clock()
    async with sessions.begin() as db:
        action = await db.get(ScheduledAction, action_id)
        if action is None:
            return
        ctx = worker_context(action)
        if action.version != expected_version or action.status != "queued":
            return
        if job_id is None or action.job_id != job_id:
            return
        job = (
            await db.execute(
                text("SELECT task_name, args FROM procrastinate_jobs WHERE id=:id"), {"id": job_id}
            )
        ).first()
        if (
            job is None
            or job.task_name != "aco.execute"
            or job.args != {"action_id": str(action_id), "expected_version": expected_version}
        ):
            return
        try:
            lease = await db.run_sync(
                lambda store: ActionService(store, instant).claim_action(ctx, action_id, expected_version)
            )
        except HTTPException as error:
            if error.status_code == 403:
                # Claim authorization refreshes under lock. A delayed duplicate
                # may now be looking at a newer claim, retry or terminal result.
                current_action = await db.run_sync(lambda store: ActionService(store).locked(ctx, action_id))
                if (
                    current_action.status != "queued"
                    or current_action.version != expected_version
                    or current_action.job_id != job_id
                ):
                    return
                from aco.features.identity.actors import require_job_binding

                try:
                    await db.run_sync(lambda store: require_job_binding(store, current_action))
                except HTTPException:
                    return
                current_action.status, current_action.last_error = "failed", str(error.detail)
                if current_action.kind == "agent_email":
                    from aco.features.tools.email import wake_email_run

                    await db.run_sync(lambda store: wake_email_run(store, current_action))
                await db.run_sync(
                    lambda store: IdentityService(store).audit(ctx, "action.blocked", action_id)
                )
            return
    ctx = ctx.model_copy(update={"lease_token": lease.token})
    invoked = False
    try:
        if (
            isinstance(executor, DomainExecutor)
            and executor.reminders is not None
            and action.kind in {"policy_due", "agent_email"}
        ):
            await executor.reminders.process_reminder_due(ctx, action_id, lease)
            return
        async with sessions.begin() as db:
            # Authority lock is held through this one effect's transaction. The
            # provider's independent durable acceptance can still outlive rollback.
            await db.run_sync(lambda store: IdentityService(store).authorize(ctx, Permission.INVOICE_READ))
            action = await db.scalar(select(ScheduledAction).where(ScheduledAction.id == action_id))
            assert action
            invoked = True
            result = await executor.execute(ctx, action, db)
            finished = at or clock()
            if result.status not in {
                ActionStatus.SUCCEEDED,
                ActionStatus.FAILED,
                ActionStatus.UNKNOWN_OUTCOME,
            }:
                raise HTTPException(409, "INVALID_EXECUTOR_RESULT")
            await db.run_sync(
                lambda store: ActionService(store, finished).complete_action(ctx, lease, result)
            )
    except (ActionFailure, AccountingFailure, EmailFailure) as provider_error:
        failure = provider_error.failure
        finished = at or clock()
        async with sessions.begin() as db:
            await _record_promise_read_failure(
                db,
                ctx,
                failure.code,
                rejection_evidence=(
                    provider_error.rejection_evidence
                    if isinstance(provider_error, AccountingFailure)
                    else None
                ),
            )
            await db.run_sync(lambda store: ActionService(store, finished).fail_action(ctx, lease, failure))
    except HTTPException:
        finished = at or clock()
        async with sessions.begin() as db:
            await _record_promise_read_failure(db, ctx, ProviderErrorCode.VALIDATION_FAILED)
            await db.run_sync(
                lambda store: ActionService(store, finished).fail_action(
                    ctx,
                    lease,
                    ProviderFailure(
                        code=ProviderErrorCode.UNKNOWN_SIDE_EFFECT
                        if invoked
                        else ProviderErrorCode.VALIDATION_FAILED,
                        retryable=False,
                        may_have_side_effect=invoked,
                    ),
                )
            )
    except Exception:
        finished = at or clock()
        # No generic exception establishes no side effect, including failed commit.
        async with sessions.begin() as db:
            await db.run_sync(
                lambda store: ActionService(store, finished).fail_action(
                    ctx,
                    lease,
                    ProviderFailure(
                        code=ProviderErrorCode.UNKNOWN_SIDE_EFFECT, retryable=False, may_have_side_effect=True
                    ),
                )
            )
