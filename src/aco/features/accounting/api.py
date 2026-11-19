"""Authenticated synthetic-only commands. No invoice/payment financial CRUD."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from aco.features.accounting.dto import DemoPaymentCommand, ReconciliationResult, SyncCursor, SyncResult
from aco.features.accounting.ingest import AccountingService
from aco.features.accounting.seed import seed_connection, simulate_payment
from aco.features.identity.api import Database, Subject
from aco.features.identity.models import ProviderConnection
from aco.features.identity.service import IdentityService
from aco.integrations.demo_accounting.adapter import AccountingRuntime

router = APIRouter(prefix="/api/workspaces/{workspace_id}")


def accounting_runtime(request: Request) -> AccountingRuntime:
    runtime = getattr(request.app.state, "accounting_runtime", None)
    if request.app.state.settings.accounting_adapter != "demo" or not isinstance(runtime, AccountingRuntime):
        raise HTTPException(503, "ACCOUNTING_NOT_CONFIGURED")
    return runtime


class PaymentRequest(DemoPaymentCommand):
    provider_connection_id: UUID = Field(strict=False)
    operation_id: UUID = Field(strict=False)


class SyncCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_connection_id: UUID
    cursor: SyncCursor | None = None


@router.post("/demo/accounting/seed", status_code=201)
async def seed(workspace_id: UUID, request: Request, subject: Subject, db: Database) -> dict[str, object]:
    accounting_runtime(request)
    ctx = await db.run_sync(lambda store: IdentityService(store).resolve_context(subject, workspace_id))
    connection = await db.scalar(
        select(ProviderConnection)
        .where(
            ProviderConnection.tenant_id == ctx.tenant_id,
            ProviderConnection.provider == "demo",
            ProviderConnection.provider_type == "accounting",
        )
        .order_by(ProviderConnection.id)
    )
    if connection is None:
        raise HTTPException(409, "COMMITTED_PROVIDER_SETUP_REQUIRED")
    key = await seed_connection(db, ctx, connection.id, request.app.state.settings.app_env)
    return {"provider_connection_id": key, "synthetic": True}


@router.post("/accounting/sync", response_model=SyncResult)
async def sync(
    workspace_id: UUID, command: SyncCommand, request: Request, subject: Subject, db: Database
) -> SyncResult:
    accounting_runtime(request)
    ctx = await db.run_sync(lambda store: IdentityService(store).resolve_context(subject, workspace_id))
    runtime = accounting_runtime(request)
    adapter = runtime.bind(db, command.provider_connection_id)
    return await AccountingService(db, adapter).sync_accounting(ctx, command.cursor)


@router.post("/demo/accounting/simulate-payment", response_model=ReconciliationResult)
async def payment(
    workspace_id: UUID, command: PaymentRequest, request: Request, subject: Subject, db: Database
) -> ReconciliationResult:
    accounting_runtime(request)
    ctx = await db.run_sync(lambda store: IdentityService(store).resolve_context(subject, workspace_id))
    return await simulate_payment(db, ctx, command, request.app.state.settings.app_env)
