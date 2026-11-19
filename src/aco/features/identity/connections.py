"""Current Owner company connection controls; local fences preserve all source history."""

from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.core.types import StrictModel
from aco.features.identity.models import ProviderConnection
from aco.features.identity.service import IdentityService, Permission


class DisconnectConnection(StrictModel):
    expected_version: int = Field(ge=1)


class ProviderConnectionView(StrictModel):
    id: UUID
    provider_type: str
    provider: str
    status: str
    version: int
    capabilities: list[str]
    can_disconnect: bool
    disconnect_reasons: list[str]
    remote_revocation: Literal["NOT_REQUESTED", "UNKNOWN"]
    history_preserved: Literal[True] = True


LOCAL_DISCONNECT_TYPES = frozenset({"email", "llm", "voice"})


def view(connection: ProviderConnection) -> ProviderConnectionView:
    reasons = []
    if connection.provider_type not in LOCAL_DISCONNECT_TYPES:
        reasons.append("PROVIDER_LIFECYCLE_UNAVAILABLE")
    if connection.status == "DISCONNECTED":
        reasons.append("ALREADY_DISCONNECTED")
    return ProviderConnectionView(
        id=connection.id,
        provider_type=connection.provider_type,
        provider=connection.provider,
        status=connection.status,
        version=connection.version,
        capabilities=connection.capabilities,
        can_disconnect=not reasons,
        disconnect_reasons=reasons,
        remote_revocation="UNKNOWN" if connection.status == "DISCONNECTED" else "NOT_REQUESTED",
    )


class ConnectionService:
    def __init__(self, db: Session):
        self.db = db

    def _owner(self, ctx: TenantContext) -> None:
        if not isinstance(ctx, TenantContext):
            raise HTTPException(403, "HUMAN_CONTEXT_REQUIRED")
        IdentityService(self.db).authorize(ctx, Permission.SETTINGS)

    def list_connections(self, ctx: TenantContext) -> list[ProviderConnectionView]:
        self._owner(ctx)
        return [
            view(row)
            for row in self.db.scalars(
                select(ProviderConnection)
                .where(ProviderConnection.tenant_id == ctx.tenant_id)
                .order_by(
                    ProviderConnection.provider_type, ProviderConnection.created_at, ProviderConnection.id
                )
            )
        ]

    def disconnect_connection(
        self, ctx: TenantContext, connection_id: UUID, command: DisconnectConnection
    ) -> ProviderConnectionView:
        self._owner(ctx)
        row = self.db.scalar(
            select(ProviderConnection)
            .where(ProviderConnection.tenant_id == ctx.tenant_id, ProviderConnection.id == connection_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise HTTPException(404, "CONNECTION_NOT_FOUND")
        if row.version != command.expected_version:
            raise HTTPException(409, "CONNECTION_VERSION_CONFLICT")
        if row.provider_type not in LOCAL_DISCONNECT_TYPES:
            raise HTTPException(409, "PROVIDER_LIFECYCLE_UNAVAILABLE")
        if row.status == "DISCONNECTED":
            return view(row)
        old_status, old_version = row.status, row.version
        row.status = "DISCONNECTED"
        row.version += 1
        # Connection status/version is already part of each current outbound,
        # ingress, agent, voice and accepted-native-read guard. Do not cancel exposed/UNKNOWN
        # actions or rewrite native IDs, originals, usage or financial records.
        IdentityService(self.db).audit(
            ctx,
            "provider.connection_disconnected",
            row.id,
            {
                "previous_status": old_status,
                "previous_version": str(old_version),
                "remote_revocation": "UNKNOWN",
                "scope": "ACO_LOCAL_AUTHORITY_ONLY",
            },
        )
        self.db.flush()
        return view(row)
