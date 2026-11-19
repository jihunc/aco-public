"""Atomically publish domain outbox records through Procrastinate's external-connection API."""

from procrastinate import App
from procrastinate.jobs import Job
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aco.core.types import StrictModel
from aco.features.actions.models import OutboxEvent, ScheduledAction
from aco.features.identity.models import Tenant, now


class DispatchBatchResult(StrictModel):
    dispatched: int
    job_ids: tuple[int, ...]


class OutboxDispatcher:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], app: App):
        self.sessions, self.app = sessions, app

    async def dispatch_outbox(self, batch_size: int) -> DispatchBatchResult:
        if not 1 <= batch_size <= 100:
            raise ValueError("Batch size must be between 1 and 100")
        ids = []
        async with self.sessions.begin() as db:
            rows = list(
                await db.scalars(
                    select(OutboxEvent)
                    .where(OutboxEvent.dispatched_at.is_(None))
                    .order_by(OutboxEvent.tenant_id, OutboxEvent.action_id, OutboxEvent.created_at)
                    .limit(batch_size)
                )
            )
            connection = await db.connection()
            raw = await connection.get_raw_connection()
            for row in rows:
                tenant = await db.scalar(
                    select(Tenant)
                    .where(Tenant.id == row.tenant_id)
                    .with_for_update(key_share=True, skip_locked=True)
                )
                if tenant is None:
                    continue
                action = await db.scalar(
                    select(ScheduledAction)
                    .where(ScheduledAction.id == row.action_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                await db.refresh(row, with_for_update=True)
                if row.dispatched_at is not None:
                    continue
                if action is None or action.version != row.action_version or action.status != "queued":
                    row.dispatched_at = now()
                    continue
                job = await self.app.job_manager.defer_job_async(
                    Job(
                        task_name="aco.execute",
                        queue="aco",
                        lock=None,
                        queueing_lock=None,
                        task_kwargs={"action_id": str(action.id), "expected_version": action.version},
                        scheduled_at=row.due_at,
                    ),
                    connection=raw.driver_connection,
                )
                assert job.id is not None
                row.job_id, row.dispatched_at, action.job_id = job.id, now(), job.id
                ids.append(job.id)
        return DispatchBatchResult(dispatched=len(ids), job_ids=tuple(ids))
