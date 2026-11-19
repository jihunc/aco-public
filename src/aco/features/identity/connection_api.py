"""Authenticated company provider history and explicit local disconnect; no wire calls."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from aco.api.auth import authenticate_request, database
from aco.features.identity.connections import ConnectionService, DisconnectConnection, ProviderConnectionView
from aco.features.identity.service import IdentityService
from aco.integrations.ports.identity import AuthenticatedSubject

router = APIRouter(prefix="/api/workspaces/{workspace_id}/provider-connections")
Subject = Annotated[AuthenticatedSubject, Depends(authenticate_request)]
Database = Annotated[AsyncSession, Depends(database, scope="function")]


@router.get("", response_model=list[ProviderConnectionView])
async def connections(workspace_id: UUID, subject: Subject, db: Database) -> list[ProviderConnectionView]:
    return await db.run_sync(
        lambda store: ConnectionService(store).list_connections(
            IdentityService(store).resolve_context(subject, workspace_id)
        )
    )


@router.post("/{connection_id}/disconnect", response_model=ProviderConnectionView)
async def disconnect_connection(
    workspace_id: UUID, connection_id: UUID, command: DisconnectConnection, subject: Subject, db: Database
) -> ProviderConnectionView:
    return await db.run_sync(
        lambda store: ConnectionService(store).disconnect_connection(
            IdentityService(store).resolve_context(subject, workspace_id), connection_id, command
        )
    )
