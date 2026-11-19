"""Explicit, CSRF-protected reporting of current humans' own recorded work."""

from uuid import UUID

from fastapi import APIRouter

from aco.features.identity.api import Database, Subject
from aco.features.identity.service import IdentityService
from aco.features.impact.handling import HandlingTimeService
from aco.features.impact.handling_dto import HandlingTimePage, HandlingTimeView, RecordHandlingTime

router = APIRouter(prefix="/api/workspaces/{workspace_id}")


@router.get("/cases/{case_id}/handling-time", response_model=HandlingTimePage)
async def read(workspace_id: UUID, case_id: UUID, subject: Subject, db: Database) -> HandlingTimePage:
    return await db.run_sync(
        lambda store: HandlingTimeService(store).read(
            IdentityService(store).resolve_context(subject, workspace_id), case_id
        )
    )


@router.post("/cases/{case_id}/handling-time", response_model=HandlingTimeView, status_code=201)
async def record(
    workspace_id: UUID, case_id: UUID, command: RecordHandlingTime, subject: Subject, db: Database
) -> HandlingTimeView:
    return await db.run_sync(
        lambda store: HandlingTimeService(store).record(
            IdentityService(store).resolve_context(subject, workspace_id), case_id, command
        )
    )
