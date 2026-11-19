"""Membership-scoped read endpoints; never invoke external accounting sync."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from sqlalchemy.orm import Session

from aco.core.types import StrictModel
from aco.features.identity.api import Database, Subject
from aco.features.identity.service import IdentityService
from aco.features.receivables.activity import InvoiceAuditPage, InvoiceAuditQuery, invoice_audit
from aco.features.receivables.queries import (
    CustomerFilter,
    CustomerView,
    CustomerWorkspace,
    InvoiceView,
    InvoiceWorkspace,
    MetricQuery,
    OperationsView,
    Page,
    ReceivableFilter,
    ReceivableQueries,
)
from aco.features.receivables.workload import WorkloadView, current_workload

router = APIRouter(prefix="/api/workspaces/{workspace_id}")


class DetailQuery(StrictModel):
    """Current detail has no supported query parameters; reject rather than ignore extras."""


@router.get("/receivables", response_model=Page[InvoiceView])
async def invoices(
    workspace_id: UUID, filters: Annotated[ReceivableFilter, Query()], subject: Subject, db: Database
) -> Page[InvoiceView]:
    ctx = await db.run_sync(lambda store: IdentityService(store).resolve_context(subject, workspace_id))
    return await ReceivableQueries(db).list_receivables(ctx, filters)


@router.get("/receivables/{invoice_id}", response_model=InvoiceWorkspace)
async def invoice(
    workspace_id: UUID,
    invoice_id: UUID,
    query: Annotated[DetailQuery, Query()],
    subject: Subject,
    db: Database,
) -> InvoiceWorkspace:
    ctx = await db.run_sync(lambda store: IdentityService(store).resolve_context(subject, workspace_id))
    return await ReceivableQueries(db).get_invoice_workspace(ctx, invoice_id)


@router.get("/overview", response_model=OperationsView)
async def overview(
    workspace_id: UUID, query: Annotated[MetricQuery, Query()], subject: Subject, db: Database
) -> OperationsView:
    ctx = await db.run_sync(lambda store: IdentityService(store).resolve_context(subject, workspace_id))
    return await ReceivableQueries(db).get_operations(ctx, query)


@router.get("/customers", response_model=Page[CustomerView])
async def customers(
    workspace_id: UUID, filters: Annotated[CustomerFilter, Query()], subject: Subject, db: Database
) -> Page[CustomerView]:
    ctx = await db.run_sync(lambda store: IdentityService(store).resolve_context(subject, workspace_id))
    return await ReceivableQueries(db).list_customers(ctx, filters)


@router.get("/customers/{customer_id}", response_model=CustomerWorkspace)
async def customer(
    workspace_id: UUID,
    customer_id: UUID,
    query: Annotated[DetailQuery, Query()],
    subject: Subject,
    db: Database,
) -> CustomerWorkspace:
    ctx = await db.run_sync(lambda store: IdentityService(store).resolve_context(subject, workspace_id))
    return await ReceivableQueries(db).get_customer(ctx, customer_id)


@router.get("/overview/workload", response_model=WorkloadView)
async def workload(
    workspace_id: UUID, query: Annotated[DetailQuery, Query()], subject: Subject, db: Database
) -> WorkloadView:
    def read(store: Session) -> WorkloadView:
        ctx = IdentityService(store).resolve_context(subject, workspace_id)
        return current_workload(store, ctx)

    return await db.run_sync(read)


@router.get("/receivables/{invoice_id}/audit", response_model=InvoiceAuditPage)
async def audit(
    workspace_id: UUID,
    invoice_id: UUID,
    query: Annotated[InvoiceAuditQuery, Query()],
    subject: Subject,
    db: Database,
) -> InvoiceAuditPage:
    def read(store: Session) -> InvoiceAuditPage:
        ctx = IdentityService(store).resolve_context(subject, workspace_id)
        return invoice_audit(store, ctx, invoice_id, query)

    return await db.run_sync(read)
