"""Current authenticated human consent evidence; no provider media accepted here."""

from uuid import UUID

from fastapi import APIRouter, Request

from aco.features.artifacts.consent import ConsentService
from aco.features.artifacts.consent_dto import (
    PrepareConsentStorage,
    RecordVoiceConsent,
    RevokeVoiceConsent,
    VoiceConsentPage,
    VoiceConsentView,
)
from aco.features.identity.api import Database, Subject
from aco.features.identity.service import IdentityService
from aco.features.voice.runtime import VoiceRuntime

router = APIRouter(prefix="/api/workspaces/{workspace_id}/voice-consents")


def runtime(request: Request) -> VoiceRuntime | None:
    value = getattr(request.app.state, "voice_runtime", None)
    return value if isinstance(value, VoiceRuntime) else None


@router.get("", response_model=VoiceConsentPage)
async def list_consents(
    workspace_id: UUID, request: Request, subject: Subject, db: Database
) -> VoiceConsentPage:
    return await db.run_sync(
        lambda store: ConsentService(
            store, runtime(request), storage_available=storage_available(request)
        ).list(IdentityService(store).resolve_context(subject, workspace_id))
    )


@router.post("", response_model=VoiceConsentView, status_code=201)
async def record(
    workspace_id: UUID, command: RecordVoiceConsent, request: Request, subject: Subject, db: Database
) -> VoiceConsentView:
    return await db.run_sync(
        lambda store: ConsentService(
            store, runtime(request), storage_available=storage_available(request)
        ).record(IdentityService(store).resolve_context(subject, workspace_id), command)
    )


@router.post("/{consent_id}/revoke", response_model=VoiceConsentView)
async def revoke(
    workspace_id: UUID,
    consent_id: UUID,
    command: RevokeVoiceConsent,
    request: Request,
    subject: Subject,
    db: Database,
) -> VoiceConsentView:
    return await db.run_sync(
        lambda store: ConsentService(
            store, runtime(request), storage_available=storage_available(request)
        ).revoke(IdentityService(store).resolve_context(subject, workspace_id), consent_id, command)
    )


def storage_available(request: Request) -> bool:
    from aco.core.settings import FakePort

    return not isinstance(request.app.state.ports.storage, FakePort)


@router.post("/storage/setup", response_model=VoiceConsentPage)
async def prepare_storage(
    workspace_id: UUID, command: PrepareConsentStorage, request: Request, subject: Subject, db: Database
) -> VoiceConsentPage:
    from fastapi import HTTPException

    from aco.features.artifacts.api import service as artifacts
    from aco.features.artifacts.preparation import prepare_private_storage

    selected = runtime(request)
    if selected is None:
        raise HTTPException(403, "CONSENT_LOCAL_PROFILE_DISABLED")
    ctx = await db.run_sync(lambda store: IdentityService(store).resolve_context(subject, workspace_id))
    return await prepare_private_storage(selected, artifacts(request).runtime, ctx, command)
