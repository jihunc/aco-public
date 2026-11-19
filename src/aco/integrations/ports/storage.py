"""Provider-neutral private object boundary; keys are derived, never caller-selected."""

from typing import Protocol
from uuid import UUID

from pydantic import Field

from aco.core.context import ActorContext
from aco.core.types import ProviderCapabilities, ProviderCost, ProviderFailure, StrictModel
from aco.integrations.ports.email_context import EmailIngressContext, EmailWebhookContext
from aco.integrations.ports.purge_context import ArtifactPurgeContext


class AuthorizedCaptureContext(StrictModel):
    """Provider-neutral minimal identity. Constructing it does not authorize application capture."""

    tenant_id: UUID
    actor_id: UUID
    correlation_id: UUID
    grant_id: UUID
    communication_id: UUID
    storage_connection_id: UUID
    artifact_ids: tuple[UUID, ...]
    action_id: UUID
    action_version: int
    job_id: int
    lease_token: UUID


StorageInvocationContext = (
    ActorContext | AuthorizedCaptureContext | EmailIngressContext | EmailWebhookContext | ArtifactPurgeContext
)


class StorageFailure(Exception):
    def __init__(self, failure: ProviderFailure, reason: str = "PROVIDER_FAILURE"):
        self.failure, self.reason = failure, reason
        super().__init__(failure.code.value)


class ObjectWrite(StrictModel):
    artifact_id: UUID
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    content_type: str
    body: bytes = Field(repr=False)


class ArtifactObjectRef(StrictModel):
    tenant_id: UUID
    connection_id: UUID
    artifact_id: UUID
    key: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    content_type: str
    size_bytes: int
    version: str | None
    encryption: str


class StorageResult[T](StrictModel):
    value: T
    tenant_id: UUID
    connection_id: UUID
    provider: str
    correlation_id: UUID
    native_operation_id: str | None = None
    provider_attempted: bool = True
    cost: ProviderCost


class StoragePort(Protocol):
    provider: str
    tenant_id: UUID
    connection_id: UUID

    async def put_private_artifact(
        self, ctx: StorageInvocationContext, command: ObjectWrite
    ) -> StorageResult[ArtifactObjectRef]: ...
    async def get_signed_read_url(
        self, ctx: StorageInvocationContext, ref: ArtifactObjectRef, ttl_seconds: int
    ) -> StorageResult[str]: ...
    async def delete_object(
        self, ctx: StorageInvocationContext, ref: ArtifactObjectRef
    ) -> StorageResult[bool]: ...
    async def get_metadata(
        self, ctx: StorageInvocationContext, ref: ArtifactObjectRef
    ) -> StorageResult[ArtifactObjectRef]: ...
    async def verify_integrity(
        self, ctx: StorageInvocationContext, ref: ArtifactObjectRef
    ) -> StorageResult[bool]: ...
    async def read_private_artifact(
        self, ctx: StorageInvocationContext, ref: ArtifactObjectRef
    ) -> StorageResult[bytes]: ...
    async def describe_capabilities(self) -> ProviderCapabilities: ...


class StorageRuntime(Protocol):
    provider: str

    def bind(self, tenant_id: UUID, connection_id: UUID) -> StoragePort: ...
