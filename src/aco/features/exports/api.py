"""Fresh synchronous download; current session/CSRF and no persistent access token."""

from uuid import UUID

from fastapi import APIRouter
from starlette.responses import Response

from aco.features.exports.service import ExportCommand, ExportService
from aco.features.identity.api import Database, Subject
from aco.features.identity.service import IdentityService

router = APIRouter(prefix="/api/workspaces/{workspace_id}/exports")


@router.post("", response_class=Response)
async def download(workspace_id: UUID, command: ExportCommand, subject: Subject, db: Database) -> Response:
    body, digest = await db.run_sync(
        lambda store: ExportService(store).download(
            IdentityService(store).resolve_context(subject, workspace_id), command
        )
    )
    return Response(
        body,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="aco-business-{workspace_id}.json"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "X-Content-SHA256": digest,
        },
    )
