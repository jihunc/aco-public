"""Authority context supplied by server authentication/membership (implemented in T03).

Constructing this value alone is not authentication. No request/header/body parser here
accepts tenant/role assertions; future routers must use the T03 dependency.
Human-only entrypoints reject ServiceContext at runtime. ActorContext is reserved
for explicitly scoped worker/accounting/internal-record seams and trusted producers.
"""

from uuid import UUID

from aco.core.types import Role, StrictModel


class TenantContext(StrictModel):
    tenant_id: UUID
    actor_id: UUID
    role: Role
    correlation_id: UUID


class ServiceContext(StrictModel):
    """Internal action-bound authority; never accepted by HTTP authentication."""

    tenant_id: UUID
    actor_id: UUID
    correlation_id: UUID
    action_id: UUID
    action_version: int
    job_id: int | None = None
    lease_token: UUID | None = None


ActorContext = TenantContext | ServiceContext
