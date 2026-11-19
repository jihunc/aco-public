"""Scoped LIVE budget lookup; caller must hold the authorized tenant serialization lock."""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.features.identity.models import ProviderConnection
from aco.features.usage.models import BudgetScope


def live_provider_scope(db: Session, tenant_id: UUID, connection: ProviderConnection) -> BudgetScope | None:
    """Keep vendor/kind exposure across replacements, including disconnected anchors.

    This is a query, never execution authority. Conflicting historical scopes need
    explicit reconciliation; selecting one would silently lose the others' exposure.
    """
    if connection.tenant_id != tenant_id:
        raise HTTPException(403, "FOREIGN_SCOPE")
    scopes = list(
        db.scalars(
            select(BudgetScope)
            .join(ProviderConnection, ProviderConnection.id == BudgetScope.provider_connection_id)
            .where(
                BudgetScope.tenant_id == tenant_id,
                BudgetScope.dimension == "provider",
                BudgetScope.synthetic.is_(False),
                ProviderConnection.tenant_id == tenant_id,
                ProviderConnection.provider == connection.provider,
                ProviderConnection.provider_type == connection.provider_type,
            )
        )
    )
    if len(scopes) > 1:
        raise HTTPException(409, "LIVE_PROVIDER_SCOPE_AMBIGUOUS")
    return scopes[0] if scopes else None
