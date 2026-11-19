"""Case outcomes use the current authenticated workspace membership."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter

from aco.features.identity.api import Database, Subject
from aco.features.identity.service import IdentityService
from aco.features.impact.case_outcome_service import CaseOutcomeService
from aco.features.impact.case_outcomes import CaseOutcomesView

router = APIRouter(prefix="/api/workspaces/{workspace_id}")


@router.get("/impact/case-outcomes", response_model=CaseOutcomesView)
async def read(
    workspace_id: UUID,
    since: datetime,
    until: datetime,
    subject: Subject,
    db: Database,
    as_of: datetime | None = None,
) -> CaseOutcomesView:
    return await db.run_sync(
        lambda store: CaseOutcomeService(store).read(
            IdentityService(store).resolve_context(subject, workspace_id), since, until, as_of
        )
    )
