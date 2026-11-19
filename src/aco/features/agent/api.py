"""Authenticated run controls and persisted replay; streaming never owns execution."""

import asyncio
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request
from starlette.responses import StreamingResponse

from aco.api.auth import authenticate_request
from aco.features.agent.schemas import AgentHistory, AgentTrigger, CancelRun, EventPage, RunPage, RunView
from aco.features.agent.service import AgentService
from aco.features.identity.api import Database, Subject
from aco.features.identity.service import IdentityService

router = APIRouter(prefix="/api/workspaces/{workspace_id}/agent-runs")


@router.post("", status_code=202, response_model=RunView)
async def start(
    workspace_id: UUID, command: AgentTrigger, request: Request, subject: Subject, db: Database
) -> RunView:
    return await db.run_sync(
        lambda store: AgentService(store).start(
            IdentityService(store).resolve_context(subject, workspace_id),
            command,
            getattr(request.app.state, "agent_live_grant_id", None),
        )
    )


@router.get("", response_model=RunPage)
async def runs(workspace_id: UUID, subject: Subject, db: Database, invoice_id: UUID | None = None) -> RunPage:
    return await db.run_sync(
        lambda store: RunPage(
            items=AgentService(store).list(
                IdentityService(store).resolve_context(subject, workspace_id), invoice_id
            )
        )
    )


@router.get("/{run_id}", response_model=RunView)
async def detail(workspace_id: UUID, run_id: UUID, subject: Subject, db: Database) -> RunView:
    return await db.run_sync(
        lambda store: AgentService(store).view(
            AgentService(store).locked(IdentityService(store).resolve_context(subject, workspace_id), run_id)
        )
    )


@router.get("/{run_id}/steps", response_model=AgentHistory)
async def steps(workspace_id: UUID, run_id: UUID, subject: Subject, db: Database) -> AgentHistory:
    return await db.run_sync(
        lambda store: AgentService(store).steps(
            IdentityService(store).resolve_context(subject, workspace_id), run_id
        )
    )


@router.get("/{run_id}/event-log", response_model=EventPage)
async def event_log(
    workspace_id: UUID, run_id: UUID, subject: Subject, db: Database, after: int = Query(default=0, ge=0)
) -> EventPage:
    return await db.run_sync(
        lambda store: AgentService(store).events(
            IdentityService(store).resolve_context(subject, workspace_id), run_id, after
        )
    )


@router.post("/{run_id}/cancel", response_model=RunView)
async def cancel(
    workspace_id: UUID, run_id: UUID, command: CancelRun, subject: Subject, db: Database
) -> RunView:
    return await db.run_sync(
        lambda store: AgentService(store).cancel(
            IdentityService(store).resolve_context(subject, workspace_id), run_id, command.expected_version
        )
    )


@router.get("/{run_id}/events")
async def events(
    workspace_id: UUID,
    run_id: UUID,
    request: Request,
    subject: Subject,
    db: Database,
    last_event_id: str | None = Header(default=None),
) -> StreamingResponse:
    cursor = 0
    if last_event_id:
        try:
            run_part, sequence = last_event_id.rsplit(":", 1)
            if UUID(run_part) != run_id or not sequence.isdecimal():
                raise ValueError()
            cursor = int(sequence)
        except ValueError:
            raise HTTPException(422, "INVALID_EVENT_CURSOR") from None
    await db.run_sync(
        lambda store: AgentService(store).events(
            IdentityService(store).resolve_context(subject, workspace_id), run_id, cursor
        )
    )

    async def stream() -> AsyncIterator[str]:
        nonlocal cursor
        for _ in range(60):
            if await request.is_disconnected():
                return
            try:
                # Recheck actual session/provider and current Membership every poll.
                current = await authenticate_request(request)
                async with request.app.state.business_sessions.begin() as session:
                    page = await session.run_sync(
                        lambda store, current=current, cursor=cursor: AgentService(store).events(
                            IdentityService(store).resolve_context(current, workspace_id), run_id, cursor
                        )
                    )
                    status = await session.run_sync(
                        lambda store, current=current: (
                            AgentService(store)
                            .locked(IdentityService(store).resolve_context(current, workspace_id), run_id)
                            .status
                        )
                    )
                for item in page.items:
                    yield f"id: {item.id}\nevent: {item.kind}\ndata: {item.model_dump_json()}\n\n"
                    cursor = item.event_sequence
                if status in {"completed", "failed", "cancelled", "partially_completed"}:
                    return
                yield ": heartbeat\n\n"
                await asyncio.sleep(1)
            except HTTPException:
                return

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
