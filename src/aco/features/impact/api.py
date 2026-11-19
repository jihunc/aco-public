"""Read-only impact router; Root owns application composition and generated client."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from aco.features.identity.api import Database, Subject
from aco.features.identity.service import IdentityService
from aco.features.impact.dto import ImpactQuery, ImpactView
from aco.features.impact.service import ImpactQueries

router = APIRouter(prefix="/api/workspaces/{workspace_id}")


@router.get("/impact", response_model=ImpactView)
async def impact(
    workspace_id: UUID, query: Annotated[ImpactQuery, Query()], subject: Subject, db: Database
) -> ImpactView:
    ctx = await db.run_sync(lambda store: IdentityService(store).resolve_context(subject, workspace_id))
    return await ImpactQueries(db).read(ctx, query)
