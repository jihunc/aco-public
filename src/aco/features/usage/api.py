"""Read usage and save draft limits; no endpoint grants approval or execution."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.features.identity.api import Database, Subject
from aco.features.identity.models import ProviderConnection
from aco.features.identity.service import IdentityService, Permission
from aco.features.usage.budgets import BudgetService
from aco.features.usage.dto import DraftLimit, UsageFilters, UsageSummary
from aco.features.usage.models import BudgetScope
from aco.features.usage.service import UsageService

router = APIRouter(prefix="/api/workspaces/{workspace_id}")


@router.get("/usage", response_model=UsageSummary)
async def summary(
    workspace_id: UUID, query: Annotated[UsageFilters, Query()], subject: Subject, db: Database
) -> UsageSummary:
    def read(store: Session) -> UsageSummary:
        ctx = IdentityService(store).resolve_context(subject, workspace_id)
        return UsageService(store).list_usage(ctx, query)

    return await db.run_sync(read)


@router.put("/usage/draft-limit", response_model=UsageSummary)
async def draft(workspace_id: UUID, command: DraftLimit, subject: Subject, db: Database) -> UsageSummary:
    def save(store: Session) -> UsageSummary:
        from fastapi import HTTPException

        service = IdentityService(store)
        ctx = service.resolve_context(subject, workspace_id)
        service.authorize(ctx, Permission.SETTINGS)
        connection = store.scalar(
            select(ProviderConnection)
            .where(ProviderConnection.tenant_id == ctx.tenant_id)
            .order_by(ProviderConnection.id)
            .limit(1)
        )
        if connection is None:
            raise HTTPException(409, "PROVIDER_SETUP_REQUIRED")
        scope = store.scalar(
            select(BudgetScope).where(
                BudgetScope.tenant_id == ctx.tenant_id,
                BudgetScope.dimension == "tenant",
                BudgetScope.synthetic.is_(False),
            )
        )
        if scope is None:
            scope = BudgetScope(
                tenant_id=ctx.tenant_id,
                provider_connection_id=None,
                dimension="tenant",
                synthetic=False,
                creator_id=ctx.actor_id,
            )
            store.add(scope)
            store.flush()
        BudgetService(store).set_limit(ctx, scope.id, command.currency, command.amount, command.requests)
        return UsageService(store).list_usage(ctx)

    return await db.run_sync(save)
