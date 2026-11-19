"""Library-owned periodic recovery; uncertainty fences effects before stalled jobs retry."""

from uuid import UUID

from procrastinate import App
from procrastinate.jobs import Job
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aco.core.types import ActionResult, ActionStatus
from aco.features.actions.models import OutboxEvent, ScheduledAction
from aco.features.actions.service import ActionLease, ActionService, worker_context
from aco.features.identity.models import now
from aco.features.identity.service import IdentityService


async def _same_stalled_job(db: AsyncSession, job: Job) -> bool:
    persisted = (
        await db.execute(
            text(
                "SELECT status::text, worker_id, attempts, task_name, args "
                "FROM procrastinate_jobs WHERE id=:id"
            ),
            {"id": job.id},
        )
    ).first()
    return bool(
        persisted is not None
        and persisted.status == "doing"
        and persisted.worker_id == job.worker_id
        and persisted.attempts == job.attempts
        and persisted.task_name == "aco.execute"
        and persisted.args == job.task_kwargs
    )


async def recover_stalled(app: App, sessions: async_sessionmaker[AsyncSession]) -> int:
    stalled = await app.job_manager.get_stalled_jobs(
        queue="aco", task_name="aco.execute", seconds_since_heartbeat=3
    )
    recovered = 0
    for job in stalled:
        try:
            action_id = UUID(str(job.task_kwargs["action_id"]))
        except (ValueError, KeyError):
            continue
        version = job.task_kwargs.get("expected_version")
        if type(version) is not int or job.task_kwargs != {
            "action_id": str(action_id),
            "expected_version": version,
        }:
            continue
        async with sessions.begin() as db:
            action = await db.get(ScheduledAction, action_id)
            if action is None:
                continue
            ctx = worker_context(action)
            action = await db.run_sync(
                lambda store, context, key: ActionService(store).locked(context, key), ctx, action_id
            )
            owned = await db.scalar(
                select(OutboxEvent.id).where(
                    OutboxEvent.tenant_id == action.tenant_id,
                    OutboxEvent.action_id == action_id,
                    OutboxEvent.action_version == version,
                    OutboxEvent.job_id == job.id,
                    OutboxEvent.dispatched_at.is_not(None),
                )
            )
            if owned is None or action.version < version:
                continue
            if not await _same_stalled_job(db, job):
                continue
            current = action.version == version
            if current and action.job_id != job.id:
                continue
            if current and action.status == "queued":
                # Reusing this JobRef would let a delayed original invocation
                # claim after retry, or acknowledge a newer execution by jobID.
                # Commit a fresh domain generation/outbox before retrying the
                # old job solely as historical no-op work.
                action.version += 1
                action.job_id = None
                db.add(
                    OutboxEvent(
                        tenant_id=action.tenant_id,
                        action_id=action.id,
                        action_version=action.version,
                        due_at=action.due_at,
                    )
                )
                await db.run_sync(
                    lambda store, context, key: IdentityService(store).audit(
                        context, "action.recovered_unclaimed", key
                    ),
                    ctx,
                    action_id,
                )
            if current and action.status == "executing" and action.lease_token is not None:
                lease = ActionLease(
                    action_id=action.id,
                    version=action.version,
                    token=action.lease_token,
                    attempt=action.attempts,
                )
                await db.run_sync(
                    lambda store, context, claimed: ActionService(store).complete_action(
                        context,
                        claimed,
                        ActionResult(
                            action_id=claimed.action_id,
                            status=ActionStatus.UNKNOWN_OUTCOME,
                            error_code="UNKNOWN_SIDE_EFFECT",
                        ),
                    ),
                    ctx,
                    lease,
                )
        # The uncertainty fence commits before the maintained retry. Serialize
        # competing maintenance under the domain lock and recheck this stalled
        # incarnation: another maintainer may already have retried/acknowledged it.
        async with sessions.begin() as db:
            action = await db.run_sync(
                lambda store, context, key: ActionService(store).locked(context, key), ctx, action_id
            )
            if not await _same_stalled_job(db, job):
                continue
            if action.version < version:
                continue
            if action.version == version:
                if action.job_id != job.id:
                    continue
                if action.status == "executing":
                    # A delayed worker may have committed a claim between our
                    # transactions without changing the library incarnation.
                    # Leave this job doing: a fresh maintenance pass must commit
                    # that current lease's uncertainty fence before any retry.
                    continue
            # Current uncertain jobs see UNKNOWN; owned historical versions see
            # the version fence. Both return without another provider submission,
            # allowing the library to acknowledge while preserving job history.
            await app.job_manager.retry_job(job, retry_at=now())
            recovered += 1
    return recovered
