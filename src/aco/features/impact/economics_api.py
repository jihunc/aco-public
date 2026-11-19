"""Current membership and CSRF-protected economics review; never provider charge editing."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from aco.features.identity.api import Database, Subject
from aco.features.identity.service import IdentityService
from aco.features.impact.cost_allocation import CostAllocationService
from aco.features.impact.economics_dto import (
    ApproveEconomicsAssumptions,
    CostAllocationView,
    EconomicsAssumptionView,
    EconomicsQuery,
    EconomicsView,
    SaveCostAllocation,
    SaveEconomicsAssumptions,
)
from aco.features.impact.economics_service import EconomicsService

router = APIRouter(prefix="/api/workspaces/{workspace_id}/impact/economics")


@router.get("", response_model=EconomicsView)
async def read(
    workspace_id: UUID, query: Annotated[EconomicsQuery, Query()], subject: Subject, db: Database
) -> EconomicsView:
    return await db.run_sync(
        lambda store: EconomicsService(store).read(
            IdentityService(store).resolve_context(subject, workspace_id), query
        )
    )


@router.post("/assumptions", response_model=EconomicsAssumptionView, status_code=201)
async def save(
    workspace_id: UUID, command: SaveEconomicsAssumptions, subject: Subject, db: Database
) -> EconomicsAssumptionView:
    return await db.run_sync(
        lambda store: EconomicsService(store).save(
            IdentityService(store).resolve_context(subject, workspace_id), command
        )
    )


@router.post("/assumptions/{assumptions_id}/approve", response_model=EconomicsAssumptionView)
async def approve(
    workspace_id: UUID,
    assumptions_id: UUID,
    command: ApproveEconomicsAssumptions,
    subject: Subject,
    db: Database,
) -> EconomicsAssumptionView:
    return await db.run_sync(
        lambda store: EconomicsService(store).approve(
            IdentityService(store).resolve_context(subject, workspace_id), assumptions_id, command
        )
    )


@router.post("/usage/{event_id}/allocation", response_model=CostAllocationView, status_code=201)
async def allocate(
    workspace_id: UUID, event_id: UUID, command: SaveCostAllocation, subject: Subject, db: Database
) -> CostAllocationView:
    return await db.run_sync(
        lambda store: CostAllocationService(store).save(
            IdentityService(store).resolve_context(subject, workspace_id), event_id, command
        )
    )
