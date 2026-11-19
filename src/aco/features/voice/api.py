"""Current-human controls and separately authenticated configured voice callback."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response

from aco.features.identity.api import Database, Subject
from aco.features.identity.service import IdentityService
from aco.features.voice.dto import (
    PrepareVoice,
    SetupVoice,
    StartVoice,
    VoiceJoinView,
    VoicePage,
    VoiceSessionView,
)
from aco.features.voice.runtime import VoiceRuntime
from aco.features.voice.service import VoiceService

router = APIRouter(prefix="/api/workspaces/{workspace_id}/voice-sessions")
callback_router = APIRouter()


def runtime(request: Request) -> VoiceRuntime:
    value = getattr(request.app.state, "voice_runtime", None)
    if not isinstance(value, VoiceRuntime):
        raise HTTPException(403, "VOICE_OFFLINE_RUNTIME_UNAVAILABLE")
    return value


@router.get("", response_model=VoicePage)
async def list_sessions(
    workspace_id: UUID, request: Request, subject: Subject, db: Database, invoice_id: UUID | None = None
) -> VoicePage:
    selected = runtime(request)
    return await db.run_sync(
        lambda store: VoiceService(store, selected).list(
            IdentityService(store).resolve_context(subject, workspace_id), invoice_id
        )
    )


@router.post("", response_model=VoiceSessionView, status_code=201)
async def prepare(
    workspace_id: UUID, command: PrepareVoice, request: Request, subject: Subject, db: Database
) -> VoiceSessionView:
    selected = runtime(request)
    return await db.run_sync(
        lambda store: VoiceService(store, selected).prepare_call(
            IdentityService(store).resolve_context(subject, workspace_id), command
        )
    )


@router.post("/setup", response_model=VoicePage)
async def setup(
    workspace_id: UUID, command: SetupVoice, request: Request, subject: Subject, db: Database
) -> VoicePage:
    selected = runtime(request)
    ctx = await db.run_sync(lambda store: IdentityService(store).resolve_context(subject, workspace_id))
    return await selected.setup(ctx, command)


@router.get("/{session_id}", response_model=VoiceSessionView)
async def detail(
    workspace_id: UUID, session_id: UUID, request: Request, subject: Subject, db: Database
) -> VoiceSessionView:
    selected = runtime(request)
    return await db.run_sync(
        lambda store: VoiceService(store, selected).get(
            IdentityService(store).resolve_context(subject, workspace_id), session_id
        )
    )


@router.post("/{session_id}/start", response_model=VoiceSessionView)
async def start(
    workspace_id: UUID,
    session_id: UUID,
    command: StartVoice,
    request: Request,
    subject: Subject,
    db: Database,
) -> VoiceSessionView:
    selected = runtime(request)
    ctx = await db.run_sync(lambda store: IdentityService(store).resolve_context(subject, workspace_id))
    return await selected.start(ctx, session_id, command.expected_version)


@callback_router.post("/api/voice/callback")
async def callback(request: Request) -> dict[str, object]:
    selected = runtime(request)
    raw = bytearray()
    async for part in request.stream():
        raw.extend(part)
        if len(raw) > 65536:
            raise HTTPException(413, "VOICE_CALLBACK_TOO_LARGE")
    return await selected.callback(bytes(raw), request.headers)


@router.post("/{session_id}/reconcile", response_model=VoiceSessionView)
async def reconcile_session(
    workspace_id: UUID, session_id: UUID, request: Request, subject: Subject, db: Database
) -> VoiceSessionView:
    from aco.features.artifacts.api import service as artifacts
    from aco.features.voice.postcall import reconcile

    ctx = await db.run_sync(lambda store: IdentityService(store).resolve_context(subject, workspace_id))
    from aco.core.settings import FakePort

    storage = None if isinstance(request.app.state.ports.storage, FakePort) else artifacts(request)
    return await reconcile(runtime(request), storage, ctx, session_id)


@router.post("/{session_id}/join", response_model=VoiceJoinView)
async def join_session(
    workspace_id: UUID,
    session_id: UUID,
    command: StartVoice,
    request: Request,
    response: Response,
    subject: Subject,
    db: Database,
) -> VoiceJoinView:
    selected = runtime(request)
    ctx = await db.run_sync(lambda store: IdentityService(store).resolve_context(subject, workspace_id))
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    return await selected.join(ctx, session_id, command.expected_version)


@router.post("/{session_id}/end", response_model=VoiceSessionView)
async def end_session(
    workspace_id: UUID,
    session_id: UUID,
    command: StartVoice,
    request: Request,
    subject: Subject,
    db: Database,
) -> VoiceSessionView:
    selected = runtime(request)
    ctx = await db.run_sync(lambda store: IdentityService(store).resolve_context(subject, workspace_id))
    return await selected.end(ctx, session_id, command.expected_version)
