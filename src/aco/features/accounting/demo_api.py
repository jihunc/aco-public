"""Current-human synthetic reset boundary; no accounting financial CRUD."""

from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import Field

from aco.features.accounting.demo_controls import (
    DemoResetCommand,
    ResetResult,
    ResetState,
    read_demo_reset,
    reset_and_restore_workspace,
)
from aco.features.identity.api import Database, Subject
from aco.features.identity.service import IdentityService

router = APIRouter(prefix="/api/workspaces/{workspace_id}/demo")


class ResetRequest(DemoResetCommand):
    request_id: UUID = Field(strict=False)


@router.post("/reset", response_model=ResetResult)
async def reset(
    workspace_id: UUID, command: ResetRequest, request: Request, subject: Subject, db: Database
) -> ResetResult:
    ctx = await db.run_sync(lambda store: IdentityService(store).resolve_context(subject, workspace_id))
    return await reset_and_restore_workspace(db, ctx, command, app_env=request.app.state.settings.app_env)


@router.get("/reset", response_model=ResetState)
async def state(workspace_id: UUID, request: Request, subject: Subject, db: Database) -> ResetState:
    return await db.run_sync(
        lambda store: read_demo_reset(
            store,
            IdentityService(store).resolve_context(subject, workspace_id),
            app_env=request.app.state.settings.app_env,
        )
    )
