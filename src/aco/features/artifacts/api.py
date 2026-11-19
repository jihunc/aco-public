"""Authenticated inert artifact delivery; no object URL leaves the application."""

import base64
import binascii
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from aco.api.auth import authenticate_request, database
from aco.core.context import TenantContext
from aco.core.settings import FakePort
from aco.features.artifacts.dto import ArtifactRef, ArtifactWrite, DeletionReceipt
from aco.features.artifacts.privacy_dto import ArtifactPrivacyView, PrivacyCommand
from aco.features.artifacts.service import ArtifactService
from aco.features.identity.service import IdentityService
from aco.integrations.ports.identity import AuthenticatedSubject

router = APIRouter()
Subject = Annotated[AuthenticatedSubject, Depends(authenticate_request)]
Database = Annotated[AsyncSession, Depends(database, scope="function")]


class UploadArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    source_kind: Literal["synthetic_fixture", "provider_message", "provider_call"]
    source_reference: str = Field(min_length=1, max_length=200)
    content_type: str = Field(min_length=1, max_length=100)
    content_base64: str = Field(max_length=15000000, repr=False)
    capture_class: Literal["synthetic_email", "synthetic_consented_voice", "real"]
    captured_at: AwareDatetime | None = None


class DeleteArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=200)


def service(request: Request) -> ArtifactService:
    runtime = request.app.state.ports.storage
    if isinstance(runtime, FakePort):
        raise HTTPException(503, "STORAGE_NOT_CONFIGURED")
    from aco.integrations.fake.voice_artifacts import SyntheticVoiceArtifacts

    environment = request.app.state.settings.app_env
    return ArtifactService(
        request.app.state.business_sessions,
        runtime,
        environment,
        voice_artifacts=SyntheticVoiceArtifacts() if environment in {"local", "demo"} else None,
    )


async def context(db: AsyncSession, subject: AuthenticatedSubject, tenant: UUID) -> TenantContext:
    return await db.run_sync(lambda store: IdentityService(store).resolve_context(subject, tenant))


@router.post("/api/workspaces/{workspace_id}/artifacts", response_model=ArtifactRef, status_code=201)
async def upload(
    workspace_id: UUID, command: UploadArtifact, request: Request, subject: Subject, db: Database
) -> ArtifactRef:
    ctx = await context(db, subject, workspace_id)
    try:
        body = base64.b64decode(command.content_base64, validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(422, "INVALID_ARTIFACT_ENCODING") from None
    return await service(request).store_artifact(
        ctx,
        ArtifactWrite(
            source_kind=command.source_kind,
            source_reference=command.source_reference,
            content_type=command.content_type,
            body=body,
            capture_class=command.capture_class,
            captured_at=command.captured_at,
        ),
    )


@router.get("/api/workspaces/{workspace_id}/artifacts/{artifact_id}", response_model=ArtifactRef)
async def describe(
    workspace_id: UUID, artifact_id: UUID, request: Request, subject: Subject, db: Database
) -> ArtifactRef:
    return await service(request).describe(await context(db, subject, workspace_id), artifact_id)


@router.get("/api/workspaces/{workspace_id}/artifacts/{artifact_id}/content", response_class=Response)
async def content(
    workspace_id: UUID, artifact_id: UUID, request: Request, subject: Subject, db: Database
) -> Response:
    try:
        value = await service(request).authorize_artifact_read(
            await context(db, subject, workspace_id), artifact_id
        )
    except HTTPException as error:
        if error.status_code == 410:
            raise HTTPException(404, "ARTIFACT_UNAVAILABLE") from None
        raise
    return Response(
        value.body,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="artifact-{value.artifact_id}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "no-store",
        },
    )


@router.delete("/api/workspaces/{workspace_id}/artifacts/{artifact_id}", response_model=DeletionReceipt)
async def delete(
    workspace_id: UUID,
    artifact_id: UUID,
    command: DeleteArtifact,
    request: Request,
    subject: Subject,
    db: Database,
) -> DeletionReceipt:
    return await service(request).delete_artifact(
        await context(db, subject, workspace_id), artifact_id, command.reason
    )


@router.post("/api/workspaces/{workspace_id}/artifacts/{artifact_id}/purge", response_model=DeletionReceipt)
async def purge(
    workspace_id: UUID, artifact_id: UUID, request: Request, subject: Subject, db: Database
) -> DeletionReceipt:
    return await service(request).execute_purge(await context(db, subject, workspace_id), artifact_id)


@router.get(
    "/api/workspaces/{workspace_id}/artifacts/{artifact_id}/privacy", response_model=ArtifactPrivacyView
)
async def privacy_detail(
    workspace_id: UUID, artifact_id: UUID, subject: Subject, db: Database
) -> ArtifactPrivacyView:
    from aco.features.artifacts.privacy import PrivacyService

    return await db.run_sync(
        lambda store: PrivacyService(store).view(
            IdentityService(store).resolve_context(subject, workspace_id), artifact_id
        )
    )


@router.post(
    "/api/workspaces/{workspace_id}/artifacts/{artifact_id}/privacy", response_model=ArtifactPrivacyView
)
async def privacy_record(
    workspace_id: UUID, artifact_id: UUID, command: PrivacyCommand, subject: Subject, db: Database
) -> ArtifactPrivacyView:
    from aco.features.artifacts.privacy import PrivacyService

    return await db.run_sync(
        lambda store: PrivacyService(store).record(
            IdentityService(store).resolve_context(subject, workspace_id), artifact_id, command
        )
    )


@router.get("/api/workspaces/{workspace_id}/artifacts/{artifact_id}/playback", response_class=Response)
async def playback(
    workspace_id: UUID, artifact_id: UUID, request: Request, subject: Subject, db: Database
) -> Response:
    try:
        value = await service(request).authorize_artifact_read(
            await context(db, subject, workspace_id), artifact_id
        )
    except HTTPException as error:
        if error.status_code == 410:
            raise HTTPException(404, "ARTIFACT_UNAVAILABLE") from None
        raise
    if value.content_type not in {"audio/wav", "audio/mpeg"}:
        raise HTTPException(404, "PLAYBACK_UNAVAILABLE")
    return Response(
        value.body,
        media_type=value.content_type,
        headers={
            "Content-Disposition": f'inline; filename="synthetic-audio-{value.artifact_id}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
    )
