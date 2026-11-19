"""Explicit synthetic cohort source command and human-scoped immutable reporting."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import Field

from aco.features.identity.api import Database, Subject
from aco.features.identity.service import IdentityService
from aco.features.impact.cohort_dto import CohortListView, RefreshCohorts, SeedCohorts
from aco.features.impact.cohort_service import CohortService
from aco.features.impact.observed_dto import (
    ChangeObserved,
    FreezeObserved,
    ObservedCohortView,
    ObservedList,
    ObservedPreview,
    ObservedSelection,
    PreviewObservedChange,
)
from aco.features.impact.observed_service import ObservedCohortService

router = APIRouter(prefix="/api/workspaces/{workspace_id}")


class SeedRequest(SeedCohorts):
    request_id: UUID = Field(strict=False)


class RefreshRequest(RefreshCohorts):
    request_id: UUID = Field(strict=False)
    expected_result_ids: list[Annotated[UUID, Field(strict=False)]] = Field(min_length=2, max_length=2)


def service(db: Database, request: Request) -> CohortService:
    settings = request.app.state.settings
    return CohortService(db, settings.app_env, settings.accounting_adapter == "demo")


@router.get("/impact/cohorts", response_model=CohortListView)
async def read(workspace_id: UUID, request: Request, subject: Subject, db: Database) -> CohortListView:
    ctx = await db.run_sync(lambda store: IdentityService(store).resolve_context(subject, workspace_id))
    return await service(db, request).read(ctx)


@router.post("/impact/cohorts/synthetic", response_model=CohortListView)
async def seed(
    workspace_id: UUID, command: SeedRequest, request: Request, subject: Subject, db: Database
) -> CohortListView:
    ctx = await db.run_sync(lambda store: IdentityService(store).resolve_context(subject, workspace_id))
    return await service(db, request).seed(ctx, command)


@router.post("/impact/cohorts/{pair_id}/refresh", response_model=CohortListView)
async def refresh(
    workspace_id: UUID,
    pair_id: UUID,
    command: RefreshRequest,
    request: Request,
    subject: Subject,
    db: Database,
) -> CohortListView:
    ctx = await db.run_sync(lambda store: IdentityService(store).resolve_context(subject, workspace_id))
    return await service(db, request).refresh(ctx, pair_id, command)


def observed_service(db: Database, request: Request) -> ObservedCohortService:
    settings = request.app.state.settings
    return ObservedCohortService(db, settings.app_env, settings.accounting_adapter == "demo")


@router.get("/impact/cohorts/observed", response_model=ObservedList)
async def observed_read(workspace_id: UUID, request: Request, subject: Subject, db: Database) -> ObservedList:
    ctx = await db.run_sync(lambda store: IdentityService(store).resolve_context(subject, workspace_id))
    return await observed_service(db, request).read(ctx)


@router.post("/impact/cohorts/observed/preview", response_model=ObservedPreview)
async def observed_preview(
    workspace_id: UUID, command: ObservedSelection, request: Request, subject: Subject, db: Database
) -> ObservedPreview:
    ctx = await db.run_sync(lambda store: IdentityService(store).resolve_context(subject, workspace_id))
    return await observed_service(db, request).preview(ctx, command)


@router.post("/impact/cohorts/observed", response_model=ObservedCohortView, status_code=201)
async def observed_freeze(
    workspace_id: UUID, command: FreezeObserved, request: Request, subject: Subject, db: Database
) -> ObservedCohortView:
    ctx = await db.run_sync(lambda store: IdentityService(store).resolve_context(subject, workspace_id))
    return await observed_service(db, request).freeze(ctx, command)


@router.post("/impact/cohorts/observed/{pair_id}/preview", response_model=ObservedPreview)
async def observed_preview_change(
    workspace_id: UUID,
    pair_id: UUID,
    command: PreviewObservedChange,
    request: Request,
    subject: Subject,
    db: Database,
) -> ObservedPreview:
    ctx = await db.run_sync(lambda store: IdentityService(store).resolve_context(subject, workspace_id))
    return await observed_service(db, request).preview_change(ctx, pair_id, command)


@router.post("/impact/cohorts/observed/{pair_id}/refresh", response_model=ObservedCohortView)
async def observed_refresh(
    workspace_id: UUID,
    pair_id: UUID,
    command: ChangeObserved,
    request: Request,
    subject: Subject,
    db: Database,
) -> ObservedCohortView:
    ctx = await db.run_sync(lambda store: IdentityService(store).resolve_context(subject, workspace_id))
    return await observed_service(db, request).change(ctx, pair_id, command, restate=False)


@router.post("/impact/cohorts/observed/{pair_id}/restate", response_model=ObservedCohortView)
async def observed_restate(
    workspace_id: UUID,
    pair_id: UUID,
    command: ChangeObserved,
    request: Request,
    subject: Subject,
    db: Database,
) -> ObservedCohortView:
    ctx = await db.run_sync(lambda store: IdentityService(store).resolve_context(subject, workspace_id))
    return await observed_service(db, request).change(ctx, pair_id, command, restate=True)
