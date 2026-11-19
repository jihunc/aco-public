"""Authenticated manual investigation scheduling and committed history reads."""

import json
from typing import Literal, cast
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.features.accounting.models import InvoiceSnapshot
from aco.features.agent.context import selected_invoices
from aco.features.agent.events import append_event, finish_step
from aco.features.agent.models import AgentEvent, AgentInvoice, AgentRun, AgentStep, AgentUsage
from aco.features.agent.schemas import (
    AgentHistory,
    AgentTrigger,
    EventPage,
    EventView,
    InvestigationResult,
    RunStatus,
    RunView,
    StepView,
)
from aco.features.identity.models import Actor, Membership, ProviderConnection
from aco.features.identity.service import IdentityService, Permission


class AgentService:
    def __init__(self, db: Session):
        self.db = db

    def locked(
        self, ctx: TenantContext, run_id: UUID, permission: Permission = Permission.INVOICE_READ
    ) -> AgentRun:
        if not isinstance(ctx, TenantContext):
            raise HTTPException(403, "HUMAN_REQUIRED")
        IdentityService(self.db).authorize(ctx, permission)
        run = self.db.scalar(
            select(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.tenant_id == ctx.tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if run is None:
            raise HTTPException(404, "RUN_NOT_FOUND")
        return run

    def start(self, ctx: TenantContext, command: AgentTrigger, live_grant_id: UUID | None = None) -> RunView:
        if not isinstance(ctx, TenantContext):
            raise HTTPException(403, "HUMAN_REQUIRED")
        tenant = IdentityService(self.db).authorize(ctx, Permission.INVESTIGATE)
        old = self.db.scalar(
            select(AgentRun).where(
                AgentRun.tenant_id == ctx.tenant_id, AgentRun.request_id == command.request_id
            )
        )
        if old:
            if (
                set(selected_invoices(self.db, old)) != set(command.invoice_ids)
                or old.objective != command.objective
                or old.initiator_id != ctx.actor_id
            ):
                raise HTTPException(409, "REQUEST_REPLAY_CONFLICT")
            return self.view(old)
        invoices = list(
            self.db.scalars(
                select(InvoiceSnapshot).where(
                    InvoiceSnapshot.tenant_id == ctx.tenant_id, InvoiceSnapshot.id.in_(command.invoice_ids)
                )
            )
        )
        if len(invoices) != len(command.invoice_ids) or any(not i.verified for i in invoices):
            raise HTTPException(422, "SELECTED_INVOICE_UNAVAILABLE")
        member = self.db.scalar(
            select(Membership).where(
                Membership.tenant_id == ctx.tenant_id, Membership.user_id == ctx.actor_id
            )
        )
        assert member is not None
        actor = self.db.scalar(
            select(Actor).where(Actor.tenant_id == ctx.tenant_id, Actor.purpose == "durable_worker")
        )
        if actor is None:
            actor = Actor(kind="service", tenant_id=ctx.tenant_id, purpose="durable_worker")
            self.db.add(actor)
            self.db.flush()
        if not actor.active:
            raise HTTPException(409, "WORKER_UNAVAILABLE")
        connection = self.db.scalar(
            select(ProviderConnection)
            .where(
                ProviderConnection.tenant_id == ctx.tenant_id,
                ProviderConnection.provider_type == "llm",
                ProviderConnection.provider == "mock",
            )
            .order_by(ProviderConnection.created_at.desc(), ProviderConnection.id)
        )
        if connection is None:
            connection = ProviderConnection(
                tenant_id=ctx.tenant_id,
                provider_type="llm",
                provider="mock",
                status="READY",
                capabilities=["structured_output", "tool_calling"],
            )
            self.db.add(connection)
            self.db.flush()
        if live_grant_id is None and connection.status != "READY":
            raise HTTPException(409, "LLM_CONNECTION_UNAVAILABLE")
        key = uuid4()
        run = AgentRun(
            id=key,
            tenant_id=ctx.tenant_id,
            request_id=command.request_id,
            initiator_id=ctx.actor_id,
            membership_version=member.version,
            worker_actor_id=actor.id,
            actor_version=actor.version,
            tenant_generation=tenant.version,
            provider_connection_id=connection.id,
            connection_version=connection.version,
            correlation_id=ctx.correlation_id,
            objective=command.objective,
            thread_id=f"{ctx.tenant_id}:{key}",
        )
        self.db.add(run)
        self.db.flush()
        for invoice in invoices:
            self.db.add(AgentInvoice(tenant_id=ctx.tenant_id, run_id=key, invoice_id=invoice.id))
        if live_grant_id is not None:
            from aco.core.live import APPROVED_LIVE_AUTHORIZATIONS
            from aco.features.agent.live import BoundedLiveGrant
            from aco.features.identity.models import now

            grant = self.db.get(BoundedLiveGrant, live_grant_id, with_for_update=True, populate_existing=True)
            if (
                grant is None
                or grant.tenant_id != ctx.tenant_id
                or grant.initiator_id != ctx.actor_id
                or grant.membership_version != member.version
                or grant.run_id is not None
                or not grant.active
                or grant.expires_at <= now()
                or grant.authorization_ref not in APPROVED_LIVE_AUTHORIZATIONS
            ):
                raise HTTPException(403, "BOUNDED_LIVE_GRANT_UNAVAILABLE")
            live_connection = self.db.get(ProviderConnection, grant.provider_connection_id)
            if (
                live_connection is None
                or live_connection.tenant_id != ctx.tenant_id
                or live_connection.provider != "openai"
                or live_connection.status != "READY"
            ):
                raise HTTPException(403, "LIVE_CONNECTION_UNAVAILABLE")
            grant.run_id = run.id
            run.authenticity, run.model_id = "LIVE", grant.model_id
            run.provider_connection_id, run.connection_version = live_connection.id, live_connection.version
        append_event(self.db, run, "queued", "Invoice investigation queued")
        IdentityService(self.db).audit(ctx, "agent.queued", run.id)
        return self.view(run)

    def view(self, run: AgentRun) -> RunView:
        usage = list(
            self.db.scalars(
                select(AgentUsage.event_id).where(
                    AgentUsage.run_id == run.id,
                    AgentUsage.tenant_id == run.tenant_id,
                    AgentUsage.event_id.is_not(None),
                )
            )
        )
        return RunView(
            id=run.id,
            source_receipt_id=run.source_receipt_id,
            source_communication_id=run.source_communication_id,
            status=cast(RunStatus, run.status),
            version=run.version,
            invoice_ids=selected_invoices(self.db, run),
            objective=run.objective,
            authenticity=cast(Literal["FAKE", "LIVE"], run.authenticity),
            model_id=run.model_id,
            model_route=run.model_route,
            result=InvestigationResult.model_validate_json(json.dumps(run.result)) if run.result else None,
            error_code=run.error_code,
            created_at=run.created_at,
            updated_at=run.updated_at,
            last_event_sequence=run.last_event_sequence,
            execution=(
                "EXECUTED"
                if run.result
                and any(
                    p.executed
                    for p in InvestigationResult.model_validate_json(json.dumps(run.result)).proposals
                )
                else "PROPOSED"
                if run.result
                else "UNAVAILABLE"
            ),
            usage_ids=[key for key in usage if key is not None],
            input_tokens=run.input_tokens,
            output_tokens=run.output_tokens,
            requests=run.requests,
            provider_attempted=run.provider_attempted,
        )

    def list(self, ctx: TenantContext, invoice_id: UUID | None = None) -> list[RunView]:
        if not isinstance(ctx, TenantContext):
            raise HTTPException(403, "HUMAN_REQUIRED")
        IdentityService(self.db).authorize(ctx, Permission.INVOICE_READ)
        query = select(AgentRun).where(AgentRun.tenant_id == ctx.tenant_id)
        if invoice_id:
            query = query.join(AgentInvoice, AgentInvoice.run_id == AgentRun.id).where(
                AgentInvoice.invoice_id == invoice_id, AgentInvoice.tenant_id == ctx.tenant_id
            )
        return [self.view(r) for r in self.db.scalars(query.order_by(AgentRun.created_at.desc()).limit(50))]

    def cancel(self, ctx: TenantContext, run_id: UUID, expected_version: int) -> RunView:
        run = self.locked(ctx, run_id, Permission.INVESTIGATE)
        if run.version != expected_version:
            raise HTTPException(409, "STALE_VERSION")
        if run.status in {"queued", "running", "awaiting_approval", "awaiting_action"}:
            for pending in self.db.scalars(
                select(AgentStep).where(
                    AgentStep.run_id == run.id,
                    AgentStep.tenant_id == ctx.tenant_id,
                    AgentStep.status == "started",
                )
            ):
                finish_step(self.db, run, pending, error="CANCELLED")
            run.status = "cancelled"
            run.version += 1
            run.lease_token = None
            run.lease_until = None
            append_event(
                self.db,
                run,
                "cancelled",
                "Investigation cancelled; any in-flight provider usage remains recorded",
            )
            IdentityService(self.db).audit(ctx, "agent.cancelled", run.id)
        return self.view(run)

    def steps(self, ctx: TenantContext, run_id: UUID) -> AgentHistory:
        run = self.locked(ctx, run_id)
        rows = self.db.scalars(
            select(AgentStep)
            .where(AgentStep.run_id == run.id, AgentStep.tenant_id == ctx.tenant_id)
            .order_by(AgentStep.step_sequence)
        )
        items = []
        for row in rows:
            usage = list(
                self.db.scalars(
                    select(AgentUsage.event_id).where(
                        AgentUsage.step_id == row.id,
                        AgentUsage.tenant_id == ctx.tenant_id,
                        AgentUsage.event_id.is_not(None),
                    )
                )
            )
            items.append(
                StepView(
                    **{k: getattr(row, k) for k in StepView.model_fields if k != "usage_ids"},
                    usage_ids=[key for key in usage if key is not None],
                )
            )
        return AgentHistory(items=items, last_event_sequence=run.last_event_sequence)

    def events(self, ctx: TenantContext, run_id: UUID, after: int) -> EventPage:
        run = self.locked(ctx, run_id)
        if after < 0 or after > run.last_event_sequence:
            raise HTTPException(422, "INVALID_EVENT_CURSOR")
        rows = self.db.scalars(
            select(AgentEvent)
            .where(
                AgentEvent.tenant_id == ctx.tenant_id,
                AgentEvent.run_id == run_id,
                AgentEvent.event_sequence > after,
            )
            .order_by(AgentEvent.event_sequence)
        )
        return EventPage(
            items=[
                EventView(
                    id=f"{run_id}:{r.event_sequence}",
                    run_id=run_id,
                    event_sequence=r.event_sequence,
                    kind=r.kind,
                    step_id=r.step_id,
                    description=r.description,
                    created_at=r.created_at,
                )
                for r in rows
            ],
            last_event_sequence=run.last_event_sequence,
        )
