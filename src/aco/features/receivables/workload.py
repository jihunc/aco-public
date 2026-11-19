"""Current company workload projection; counts never imply cash, delivery or resolution success."""

from datetime import datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import SQLColumnExpression, func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from aco.core.context import TenantContext
from aco.core.types import StrictModel
from aco.features.accounting.models import InvoiceSnapshot
from aco.features.actions.models import ScheduledAction
from aco.features.agent.models import AgentRun
from aco.features.cases.models import CollectionCase
from aco.features.identity.models import ProviderConnection, Tenant, now
from aco.features.identity.service import IdentityService, Permission
from aco.features.promises.models import PaymentPromise
from aco.features.review.models import ApprovalRequest, CollectionTask


class WorkloadSource(StrictModel):
    connection_id: UUID
    provider: str
    status: str
    imported_invoice_count: int
    oldest_import_observation_at: datetime | None


class WorkloadView(StrictModel):
    as_of_at: datetime
    open_tasks: int
    pending_approvals: int
    unresolved_cases: int
    active_promises: int
    unconfirmed_promises: int
    missed_promises: int
    active_investigations: int
    completed_investigations: int
    completed_live_investigations: int
    failed_investigations: int
    stopped_actions: int
    outbound_paused: bool
    automation_enabled: bool
    accounting_sources: list[WorkloadSource]


def current_workload(db: Session, ctx: TenantContext) -> WorkloadView:
    if not isinstance(ctx, TenantContext):
        raise HTTPException(403, "HUMAN_REQUIRED")
    identity = IdentityService(db)
    identity.authorize(ctx, Permission.INVOICE_READ)
    tenant = db.get(Tenant, ctx.tenant_id, populate_existing=True)
    assert tenant is not None

    def count(field: SQLColumnExpression[UUID], *criteria: ColumnElement[bool]) -> ColumnElement[int]:
        return select(func.count()).where(field == ctx.tenant_id, *criteria).scalar_subquery()

    # A single SQL statement gives the related operational counts one MVCC snapshot.
    values = (
        db.execute(
            select(
                count(
                    CollectionTask.tenant_id,
                    CollectionTask.deleted_at.is_(None),
                    CollectionTask.status != "done",
                ).label("open_tasks"),
                count(ApprovalRequest.tenant_id, ApprovalRequest.status == "pending").label(
                    "pending_approvals"
                ),
                count(CollectionCase.tenant_id, CollectionCase.status.not_in(["RESOLVED", "CLOSED"])).label(
                    "unresolved_cases"
                ),
                count(PaymentPromise.tenant_id, PaymentPromise.status == "ACTIVE").label("active_promises"),
                count(
                    PaymentPromise.tenant_id, PaymentPromise.status.in_(["CAPTURED", "NEEDS_CONFIRMATION"])
                ).label("unconfirmed_promises"),
                count(PaymentPromise.tenant_id, PaymentPromise.status == "MISSED").label("missed_promises"),
                count(
                    AgentRun.tenant_id,
                    AgentRun.status.in_(["queued", "running", "awaiting_approval", "awaiting_action"]),
                ).label("active_investigations"),
                count(AgentRun.tenant_id, AgentRun.status == "completed").label("completed_investigations"),
                count(
                    AgentRun.tenant_id, AgentRun.status == "completed", AgentRun.authenticity == "LIVE"
                ).label("completed_live_investigations"),
                count(AgentRun.tenant_id, AgentRun.status.in_(["failed", "partially_completed"])).label(
                    "failed_investigations"
                ),
                count(
                    ScheduledAction.tenant_id,
                    ScheduledAction.status.in_(["failed", "dead_letter", "unknown_outcome"]),
                ).label("stopped_actions"),
            )
        )
        .mappings()
        .one()
    )
    sources = db.execute(
        select(
            ProviderConnection.id.label("connection_id"),
            ProviderConnection.provider,
            ProviderConnection.status,
            func.count(InvoiceSnapshot.id).label("imported_invoice_count"),
            func.min(InvoiceSnapshot.observed_at).label("oldest_import_observation_at"),
        )
        .outerjoin(
            InvoiceSnapshot,
            (InvoiceSnapshot.provider_connection_id == ProviderConnection.id)
            & (InvoiceSnapshot.tenant_id == ctx.tenant_id),
        )
        .where(
            ProviderConnection.tenant_id == ctx.tenant_id, ProviderConnection.provider_type == "accounting"
        )
        .group_by(ProviderConnection.id)
        .order_by(ProviderConnection.id)
    ).mappings()
    return WorkloadView(
        **dict(values),
        as_of_at=now(),
        outbound_paused=tenant.outbound_paused,
        automation_enabled=tenant.automation_enabled,
        accounting_sources=[WorkloadSource.model_validate(dict(row)) for row in sources],
    )
