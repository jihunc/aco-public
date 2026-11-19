"""Purge-only identity; constructing values never authorizes storage or business operations."""

from uuid import UUID

from aco.core.types import StrictModel


class ArtifactPurgeContext(StrictModel):
    tenant_id: UUID
    actor_id: UUID
    correlation_id: UUID
    purge_id: UUID
    artifact_id: UUID
    storage_connection_id: UUID
    job_id: int
    job_version: int
    lease_token: UUID
