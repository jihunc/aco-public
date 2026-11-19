"""Durable privacy deletion uses its own persisted authority, never an Owner impersonation."""

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from fastapi import HTTPException
from procrastinate import App
from procrastinate.jobs import Job
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from aco.features.artifacts.models import Artifact, ArtifactPurge
from aco.features.identity.models import Actor, ProviderConnection, Tenant, now
from aco.integrations.ports.purge_context import ArtifactPurgeContext


def current_purge(db: Session, ctx: ArtifactPurgeContext) -> Tenant:
    tenant = db.scalar(select(Tenant).where(Tenant.id == ctx.tenant_id).with_for_update(key_share=True))
    row = db.get(ArtifactPurge, ctx.purge_id, populate_existing=True, with_for_update=True)
    artifact = db.get(Artifact, ctx.artifact_id, populate_existing=True, with_for_update=True)
    actor = db.get(Actor, ctx.actor_id, populate_existing=True)
    storage = db.get(ProviderConnection, ctx.storage_connection_id, populate_existing=True)
    job = db.execute(
        text("SELECT task_name,args,status::text,worker_id FROM procrastinate_jobs WHERE id=:id"),
        {"id": ctx.job_id},
    ).first()
    worker = (
        None
        if job is None or job.worker_id is None
        else db.execute(
            text("SELECT last_heartbeat FROM procrastinate_workers WHERE id=:id"), {"id": job.worker_id}
        ).first()
    )
    if (
        tenant is None
        or row is None
        or artifact is None
        or actor is None
        or storage is None
        or row.tenant_id != ctx.tenant_id
        or artifact.tenant_id != ctx.tenant_id
        or row.artifact_id != artifact.id
        or row.worker_actor_id != actor.id
        or actor.tenant_id != ctx.tenant_id
        or actor.kind != "service"
        or actor.purpose != "durable_worker"
        or not actor.active
        or artifact.provider_connection_id != storage.id
        or storage.tenant_id != ctx.tenant_id
        or storage.provider_type != "storage"
        or storage.status != "READY"
        or row.job_id != ctx.job_id
        or row.job_version != ctx.job_version
        or row.lease_token != ctx.lease_token
        or row.lease_until is None
        or row.lease_until <= now()
        or row.status != "executing"
        or row.failure_code in {"OWNER_DELETION_REVIEW_REQUIRED", "VOICE_PROVIDER_PURGE_UNAVAILABLE"}
        or ctx.correlation_id != row.id
        or artifact.legal_hold_state != "none"
        or row.due_at > now()
        or (artifact.expires_at > now() and artifact.revoked_at is None and not artifact.consent_revoked)
        or job is None
        or job.task_name != "aco.purge_artifact"
        or job.status != "doing"
        or job.args != {"purge_id": str(row.id), "expected_version": row.job_version}
        or worker is None
        or worker.last_heartbeat < now() - timedelta(seconds=90)
    ):
        raise HTTPException(403, "ARTIFACT_PURGE_AUTHORITY_INVALID")
    return tenant


async def dispatch_purges(sessions: async_sessionmaker[AsyncSession], app: App) -> int:
    count = 0
    async with sessions.begin() as db:
        rows = list(
            await db.scalars(
                select(ArtifactPurge)
                .join(
                    Artifact,
                    (Artifact.id == ArtifactPurge.artifact_id)
                    & (Artifact.tenant_id == ArtifactPurge.tenant_id),
                )
                .where(
                    Artifact.legal_hold_state == "none",
                    or_(
                        ArtifactPurge.failure_code.is_(None),
                        ArtifactPurge.failure_code.not_in(
                            [
                                "OWNER_DELETION_REVIEW_REQUIRED",
                                "VOICE_PROVIDER_PURGE_UNAVAILABLE",
                                "WORKER_REVOKED",
                            ]
                        ),
                    ),
                    ArtifactPurge.status.in_(["scheduled", "purge_pending", "blocked"]),
                    ArtifactPurge.due_at <= now(),
                    ArtifactPurge.job_id.is_(None),
                )
                .order_by(ArtifactPurge.due_at, ArtifactPurge.id)
                .limit(20)
            )
        )
        raw = await (await db.connection()).get_raw_connection()
        for candidate in rows:
            tenant = await db.scalar(
                select(Tenant)
                .where(Tenant.id == candidate.tenant_id)
                .with_for_update(key_share=True, skip_locked=True)
            )
            if tenant is None:
                continue
            row = await db.get(ArtifactPurge, candidate.id, with_for_update=True, populate_existing=True)
            if (
                row is None
                or row.job_id is not None
                or row.status not in {"scheduled", "purge_pending", "blocked"}
                or row.due_at > now()
                or row.failure_code in {"OWNER_DELETION_REVIEW_REQUIRED", "VOICE_PROVIDER_PURGE_UNAVAILABLE"}
            ):
                continue
            artifact = await db.get(Artifact, row.artifact_id, with_for_update=True)
            if artifact is None or artifact.legal_hold_state != "none":
                row.status, row.failure_code = "blocked", "RETENTION_REVIEW_REQUIRED"
                continue
            actor = await db.scalar(
                select(Actor).where(
                    Actor.tenant_id == row.tenant_id,
                    Actor.purpose == "durable_worker",
                    Actor.active.is_(True),
                )
            )
            if actor is None:
                # The same canonical tenant worker identity used by maintained actions.
                old = await db.scalar(
                    select(Actor).where(Actor.tenant_id == row.tenant_id, Actor.purpose == "durable_worker")
                )
                if old is not None:
                    row.status, row.failure_code = "blocked", "WORKER_REVOKED"
                    continue
                actor = Actor(tenant_id=row.tenant_id, kind="service", purpose="durable_worker", active=True)
                db.add(actor)
                await db.flush()
            job = await app.job_manager.defer_job_async(
                Job(
                    task_name="aco.purge_artifact",
                    queue="aco",
                    lock=None,
                    queueing_lock=None,
                    task_kwargs={"purge_id": str(row.id), "expected_version": row.job_version},
                ),
                connection=raw.driver_connection,
            )
            row.job_id, row.worker_actor_id, row.status = job.id, actor.id, "queued"
            count += 1
    return count


async def recover_purges(sessions: async_sessionmaker[AsyncSession]) -> int:
    count = 0
    async with sessions.begin() as db:
        rows = list(
            await db.scalars(
                select(ArtifactPurge)
                .where(
                    ArtifactPurge.job_id.is_not(None),
                    # Older denied reads could leave pending work attached to a finished job.
                    ArtifactPurge.status.in_(["queued", "executing", "blocked", "purge_pending"]),
                )
                .limit(100)
                .with_for_update(skip_locked=True)
            )
        )
        for row in rows:
            job = (
                await db.execute(
                    text("SELECT status::text FROM procrastinate_jobs WHERE id=:id"), {"id": row.job_id}
                )
            ).first()
            expired = row.lease_until is not None and row.lease_until <= now()
            if job is None or job.status in {"failed", "succeeded", "cancelled", "aborted"} or expired:
                row.job_id, row.lease_token, row.lease_until = None, None, None
                row.job_version += 1
                row.status = "purge_pending"
                row.due_at = max(row.due_at, now())
                count += 1
    return count


async def execute_purge(
    sessions: async_sessionmaker[AsyncSession],
    purge_id: UUID,
    expected_version: int,
    job_id: int,
    artifacts: "ArtifactService",
) -> None:
    async with sessions.begin() as db:
        candidate = await db.get(ArtifactPurge, purge_id)
        if candidate is None:
            return
        await db.scalar(
            select(Tenant).where(Tenant.id == candidate.tenant_id).with_for_update(key_share=True)
        )
        row = await db.get(ArtifactPurge, purge_id, with_for_update=True, populate_existing=True)
        if (
            row is None
            or row.status != "queued"
            or row.job_id != job_id
            or row.job_version != expected_version
        ):
            return
        artifact = await db.get(Artifact, row.artifact_id)
        assert artifact and row.worker_actor_id
        if artifact.legal_hold_state != "none":
            row.status, row.failure_code = "blocked", "RETENTION_REVIEW_REQUIRED"
            return
        row.status, row.lease_token, row.lease_until = "executing", uuid4(), now() + timedelta(seconds=90)
        ctx = ArtifactPurgeContext(
            tenant_id=row.tenant_id,
            actor_id=row.worker_actor_id,
            correlation_id=row.id,
            purge_id=row.id,
            artifact_id=row.artifact_id,
            storage_connection_id=artifact.provider_connection_id,
            job_id=job_id,
            job_version=row.job_version,
            lease_token=row.lease_token,
        )
        await db.flush()
        await db.run_sync(lambda store: current_purge(store, ctx))
    await artifacts.execute_purge(ctx, ctx.artifact_id)


if TYPE_CHECKING:
    from aco.features.artifacts.service import ArtifactService
