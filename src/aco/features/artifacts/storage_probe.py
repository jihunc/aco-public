"""Shared real APP storage readiness proof with exact owned-object cleanup."""

from collections.abc import Awaitable, Callable
from hashlib import sha256
from typing import Protocol
from uuid import UUID, uuid5

from fastapi import HTTPException

from aco.core.context import TenantContext
from aco.integrations.ports.storage import ArtifactObjectRef, ObjectWrite, StoragePort, StorageResult


class ProbeObserver(Protocol):
    async def __call__[T](
        self, operation: str, call: Callable[[], Awaitable[StorageResult[T]]], *, cleanup: bool = False
    ) -> StorageResult[T]: ...


async def probe_private_storage(
    port: StoragePort, ctx: TenantContext, request_id: UUID, observe: ProbeObserver
) -> list[str]:
    if port.tenant_id != ctx.tenant_id or port.provider != "s3":
        raise HTTPException(503, "APP_STORAGE_BINDING_INVALID")
    capabilities = await port.describe_capabilities()
    if not {"private", "encryption", "integrity", "authenticated_binary_read"}.issubset(
        capabilities.features
    ):
        raise HTTPException(503, "APP_STORAGE_CAPABILITY_REQUIRED")
    artifact_id = uuid5(request_id, "private-app-storage-probe")
    body = b"ACO SYNTHETIC INBOX STORAGE PROBE " + artifact_id.bytes
    digest = sha256(body).hexdigest()
    write = ObjectWrite(
        artifact_id=artifact_id, sha256=digest, content_type="application/octet-stream", body=body
    )
    ref = ArtifactObjectRef(
        tenant_id=ctx.tenant_id,
        connection_id=port.connection_id,
        artifact_id=artifact_id,
        key=f"tenants/{ctx.tenant_id}/artifacts/{artifact_id}/{digest}",
        sha256=digest,
        content_type=write.content_type,
        size_bytes=len(body),
        version=None,
        encryption="AES256",
    )
    try:
        written = await observe("put_private_artifact", lambda: port.put_private_artifact(ctx, write))
        if (
            written.value.tenant_id,
            written.value.connection_id,
            written.value.artifact_id,
            written.value.sha256,
            written.value.encryption,
        ) != (ctx.tenant_id, port.connection_id, artifact_id, digest, "AES256"):
            raise HTTPException(503, "APP_STORAGE_WRITE_UNVERIFIED")
        verified = await observe("verify_integrity", lambda: port.verify_integrity(ctx, ref))
        if verified.value is not True:
            raise HTTPException(503, "APP_STORAGE_INTEGRITY_FAILED")
        read = await observe("read_private_artifact", lambda: port.read_private_artifact(ctx, ref))
        if read.value != body:
            raise HTTPException(503, "APP_STORAGE_READBACK_FAILED")
    finally:
        deleted = await observe("delete_object", lambda: port.delete_object(ctx, ref), cleanup=True)
        if deleted.value is not True:
            raise HTTPException(503, "APP_STORAGE_CLEANUP_UNCONFIRMED")
    return sorted(capabilities.features)
