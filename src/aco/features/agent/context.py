"""Run-specific authority; never a generic ActorContext or an accounting/storage grant."""

from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.core.types import Role
from aco.features.agent.models import AgentInvoice, AgentRun
from aco.features.identity.models import Actor, Membership, ProviderConnection, Tenant, now
from aco.features.identity.service import IdentityService, Permission


@dataclass(frozen=True)
class RunAuthority:
    tenant_id: UUID
    actor_id: UUID
    correlation_id: UUID
    run_id: UUID
    job_id: int
    job_version: int
    incarnation: int
    lease_token: UUID


def current_run(db: Session, ctx: RunAuthority) -> AgentRun:
    tenant = db.scalar(
        select(Tenant)
        .where(Tenant.id == ctx.tenant_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    run = db.scalar(
        select(AgentRun)
        .where(AgentRun.id == ctx.run_id, AgentRun.tenant_id == ctx.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if run is None or tenant is None:
        raise HTTPException(403, "RUN_AUTHORITY_DENIED")
    actor = db.get(Actor, ctx.actor_id, populate_existing=True)
    member = db.scalar(
        select(Membership)
        .where(Membership.tenant_id == ctx.tenant_id, Membership.user_id == run.initiator_id)
        .execution_options(populate_existing=True)
    )
    connection = db.get(ProviderConnection, run.provider_connection_id, populate_existing=True)
    if (
        run.status != "running"
        or run.job_id != ctx.job_id
        or run.job_version != ctx.job_version
        or run.incarnation != ctx.incarnation
        or run.lease_token != ctx.lease_token
        or run.lease_until is None
        or run.lease_until <= now()
        or run.tenant_generation != tenant.version
        or run.worker_actor_id != ctx.actor_id
        or run.correlation_id != ctx.correlation_id
        or actor is None
        or not actor.active
        or actor.version != run.actor_version
        or actor.kind != "service"
        or actor.tenant_id != ctx.tenant_id
        or actor.purpose != "durable_worker"
        or member is None
        or not member.active
        or member.version != run.membership_version
        or connection is None
        or connection.tenant_id != ctx.tenant_id
        or connection.version != run.connection_version
        or connection.provider_type != "llm"
        or connection.status != "READY"
    ):
        raise HTTPException(403, "RUN_AUTHORITY_REVOKED")
    IdentityService(db).authorize(
        TenantContext(
            tenant_id=ctx.tenant_id,
            actor_id=member.user_id,
            role=Role(member.role),
            correlation_id=ctx.correlation_id,
        ),
        Permission.INVESTIGATE,
    )
    job = db.execute(
        text("SELECT task_name,args,status FROM procrastinate_jobs WHERE id=:id"), {"id": ctx.job_id}
    ).first()
    if (
        job is None
        or job.task_name != "aco.investigate"
        or job.args != {"run_id": str(run.id), "expected_version": run.job_version}
        or job.status != "doing"
    ):
        raise HTTPException(403, "INVALID_RUN_JOB_BINDING")
    if run.source_receipt_id is not None:
        from aco.features.agent.ingress import validate_inbound_run

        validate_inbound_run(db, run)
    return run


def selected_invoices(db: Session, run: AgentRun) -> list[UUID]:
    return list(
        db.scalars(
            select(AgentInvoice.invoice_id)
            .where(AgentInvoice.run_id == run.id, AgentInvoice.tenant_id == run.tenant_id)
            .order_by(AgentInvoice.invoice_id)
        )
    )
