"""Role-free identity values; application validators must prove persisted authority."""

from uuid import UUID

from aco.core.types import StrictModel


class EmailWebhookContext(StrictModel):
    tenant_id: UUID
    actor_id: UUID
    correlation_id: UUID
    activation_id: UUID
    email_connection_id: UUID
    raw_sha256: str


class EmailIngressContext(StrictModel):
    tenant_id: UUID
    actor_id: UUID
    correlation_id: UUID
    activation_id: UUID
    receipt_id: UUID
    receipt_version: int
    email_connection_id: UUID
    message_id: str
    job_id: int
    lease_token: UUID
    storage_connection_id: UUID
    artifact_ids: tuple[UUID, ...] = ()
