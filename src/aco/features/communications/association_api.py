"""Current authenticated review of retained originals; standard middleware supplies CSRF."""

from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy.orm import Session

from aco.features.communications.association import link_communication, link_review
from aco.features.communications.association_dto import CommunicationLinkReview, LinkCommunication
from aco.features.communications.dto import CommunicationView
from aco.features.communications.queries import CommunicationQueries
from aco.features.identity.api import Database, Subject
from aco.features.identity.service import IdentityService

router = APIRouter(prefix="/api/workspaces/{workspace_id}")


@router.get("/communications/{communication_id}/link-review", response_model=CommunicationLinkReview)
async def review(
    workspace_id: UUID, communication_id: UUID, subject: Subject, db: Database
) -> CommunicationLinkReview:
    return await db.run_sync(
        lambda store: link_review(
            store,
            IdentityService(store).resolve_context(subject, workspace_id),
            communication_id,
        )
    )


@router.post("/communications/{communication_id}/invoice-links", response_model=CommunicationView)
async def link(
    workspace_id: UUID, communication_id: UUID, command: LinkCommunication, subject: Subject, db: Database
) -> CommunicationView:
    if command.evidence.communication_id != communication_id:
        raise HTTPException(422, "COMMUNICATION_ID_MISMATCH")

    def perform(store: Session) -> CommunicationView:
        ctx = IdentityService(store).resolve_context(subject, workspace_id)
        result = link_communication(store, ctx, command)
        return CommunicationQueries(store, ctx).get(result)

    return await db.run_sync(perform)
