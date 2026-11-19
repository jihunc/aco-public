"""Maintained S3 SDK behind a bounded AnyIO bridge; no URL fetching in domain code."""

import hashlib
import ipaddress
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from functools import partial
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import anyio
import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import SecretStr

from aco.core.context import TenantContext
from aco.core.types import CostQuality, ProviderCapabilities, ProviderCost, ProviderErrorCode, ProviderFailure
from aco.integrations.ports.email_context import EmailIngressContext
from aco.integrations.ports.purge_context import ArtifactPurgeContext
from aco.integrations.ports.storage import (
    ArtifactObjectRef,
    AuthorizedCaptureContext,
    ObjectWrite,
    StorageFailure,
    StorageInvocationContext,
    StorageResult,
)


def failure(
    code: ProviderErrorCode,
    reason: str = "PROVIDER_FAILURE",
    *,
    native: str | None = None,
    uncertain: bool = False,
    retry_after: timedelta | None = None,
) -> StorageFailure:
    return StorageFailure(
        ProviderFailure(
            code=code,
            retryable=code == ProviderErrorCode.TEMPORARILY_UNAVAILABLE and not uncertain,
            may_have_side_effect=uncertain,
            request_id=native,
            retry_after=retry_after,
        ),
        reason,
    )


def _retry_after(headers: Any) -> timedelta | None:
    """Observe a valid HTTP delay without granting retry or mutation authority."""
    if not isinstance(headers, dict):
        return None
    value = next(
        (value for key, value in headers.items() if isinstance(key, str) and key.lower() == "retry-after"),
        None,
    )
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        if value.isascii() and value.isdigit():
            return timedelta(seconds=int(value))
        deadline = parsedate_to_datetime(value)
        if deadline.tzinfo is None:
            return None
        return max(timedelta(0), deadline - datetime.now(UTC))
    except (ValueError, TypeError, OverflowError):
        return None


def object_key(tenant: UUID, artifact: UUID, sha256: str) -> str:
    return f"tenants/{tenant}/artifacts/{artifact}/{sha256}"


class StorageRuntime:
    provider = "s3"

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key: SecretStr,
        secret_key: SecretStr,
        local_endpoint_ip: str | None = None,
    ):
        self.configuration: dict[str, Any] = dict(
            endpoint=endpoint,
            bucket=bucket,
            access_key=access_key,
            secret_key=secret_key,
            local_endpoint_ip=local_endpoint_ip,
        )

    def bind(self, tenant_id: UUID, connection_id: UUID) -> "S3StorageAdapter":
        return S3StorageAdapter(tenant_id=tenant_id, connection_id=connection_id, **self.configuration)


class S3StorageAdapter:
    provider = "s3"

    def __init__(
        self,
        *,
        tenant_id: UUID,
        connection_id: UUID,
        endpoint: str,
        bucket: str,
        access_key: SecretStr,
        secret_key: SecretStr,
        local_endpoint_ip: str | None = None,
    ):
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Invalid storage endpoint")
        if local_endpoint_ip is not None:
            address = ipaddress.ip_address(local_endpoint_ip)
            if (
                not isinstance(address, ipaddress.IPv4Address)
                or not address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_multicast
                or address.is_unspecified
            ):
                raise ValueError("Invalid trusted local storage address")
        exact_local = (
            local_endpoint_ip is not None and parsed.hostname == local_endpoint_ip and parsed.port == 8333
        )
        if (
            parsed.scheme == "http"
            and parsed.hostname not in {"127.0.0.1", "localhost", "storage"}
            and not exact_local
        ):
            raise ValueError("Storage requires TLS outside local service topology")
        if not bucket or not access_key.get_secret_value() or not secret_key.get_secret_value():
            raise ValueError("Private storage credentials required")
        self.tenant_id, self.connection_id, self.bucket = tenant_id, connection_id, bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key.get_secret_value(),
            aws_secret_access_key=secret_key.get_secret_value(),
            region_name="us-east-1",
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"total_max_attempts": 1},
                connect_timeout=3,
                read_timeout=5,
            ),
        )

    def _scope(
        self, ctx: StorageInvocationContext, ref: ArtifactObjectRef | None = None, *, purge: bool = False
    ) -> None:
        if (
            not isinstance(
                ctx, TenantContext | AuthorizedCaptureContext | EmailIngressContext | ArtifactPurgeContext
            )
            or ctx.tenant_id != self.tenant_id
        ):
            raise failure(ProviderErrorCode.VALIDATION_FAILED, "FOREIGN_SCOPE")
        if isinstance(ctx, ArtifactPurgeContext) and (
            not purge
            or ref is None
            or ref.artifact_id != ctx.artifact_id
            or ctx.storage_connection_id != self.connection_id
        ):
            raise failure(ProviderErrorCode.VALIDATION_FAILED, "PURGE_ONLY_CONTEXT")
        if isinstance(ctx, AuthorizedCaptureContext | EmailIngressContext) and (
            ctx.storage_connection_id != self.connection_id
            or (ref is not None and ref.artifact_id not in ctx.artifact_ids)
        ):
            raise failure(ProviderErrorCode.VALIDATION_FAILED, "CAPTURE_SCOPE")
        if ref and (
            ref.tenant_id != self.tenant_id
            or ref.connection_id != self.connection_id
            or ref.key != object_key(self.tenant_id, ref.artifact_id, ref.sha256)
        ):
            raise failure(ProviderErrorCode.VALIDATION_FAILED, "FOREIGN_SCOPE")

    def _sdk(self, method: str, *, mutation: bool = False, **kwargs: Any) -> dict[str, Any]:
        try:
            response: dict[str, Any] = getattr(self.client, method)(Bucket=self.bucket, **kwargs)
            return response
        except ClientError as error:
            error_response = error.response
            native = error_response.get("ResponseMetadata", {}).get("RequestId")
            retry_after = _retry_after(error_response.get("ResponseMetadata", {}).get("HTTPHeaders"))
            code = error_response.get("Error", {}).get("Code")
            if code in {"NoSuchKey", "NotFound", "404"}:
                raise failure(
                    ProviderErrorCode.VALIDATION_FAILED,
                    "OBJECT_MISSING",
                    native=native,
                    retry_after=retry_after,
                ) from None
            if code in {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "ExpiredToken", "403"}:
                raise failure(
                    ProviderErrorCode.AUTH_EXPIRED, native=native, retry_after=retry_after
                ) from None
            if code in {
                "NotImplemented",
                "UnsupportedOperation",
                "ServerSideEncryptionConfigurationNotFoundError",
            }:
                raise failure(
                    ProviderErrorCode.UNSUPPORTED_CAPABILITY, native=native, retry_after=retry_after
                ) from None
            if code in {"SlowDown", "Throttling", "429"}:
                raise failure(
                    ProviderErrorCode.RATE_LIMITED, native=native, retry_after=retry_after
                ) from None
            uncertain = (
                mutation and int(error_response.get("ResponseMetadata", {}).get("HTTPStatusCode", 500)) >= 500
            )
            raise failure(
                ProviderErrorCode.UNKNOWN_SIDE_EFFECT
                if uncertain
                else ProviderErrorCode.TEMPORARILY_UNAVAILABLE,
                native=native,
                retry_after=retry_after,
                uncertain=uncertain,
            ) from None
        except BotoCoreError:
            raise failure(
                ProviderErrorCode.UNKNOWN_SIDE_EFFECT
                if mutation
                else ProviderErrorCode.TEMPORARILY_UNAVAILABLE,
                uncertain=mutation,
            ) from None

    def _result(
        self,
        ctx: StorageInvocationContext,
        value: Any,
        response: dict[str, Any] | None = None,
        *,
        attempted: bool = True,
    ) -> StorageResult[Any]:
        return StorageResult(
            value=value,
            tenant_id=self.tenant_id,
            connection_id=self.connection_id,
            provider="s3",
            correlation_id=ctx.correlation_id,
            native_operation_id=(response or {}).get("ResponseMetadata", {}).get("RequestId"),
            provider_attempted=attempted,
            cost=ProviderCost(amount=None, currency="USD", quality=CostQuality.UNKNOWN),
        )

    def _metadata(self, ref: ArtifactObjectRef) -> tuple[ArtifactObjectRef, dict[str, Any]]:
        response = self._sdk("head_object", Key=ref.key)
        if response.get("ServerSideEncryption") != "AES256":
            raise failure(ProviderErrorCode.UNSUPPORTED_CAPABILITY, "ENCRYPTION_UNVERIFIED")
        if (
            response.get("Metadata", {}).get("sha256") != ref.sha256
            or response.get("ContentLength") != ref.size_bytes
            or response.get("ContentType") != ref.content_type
        ):
            raise failure(ProviderErrorCode.VALIDATION_FAILED, "INTEGRITY_FAILED")
        version = response.get("VersionId") or response.get("ETag")
        if ref.version is not None and version != ref.version:
            raise failure(ProviderErrorCode.VALIDATION_FAILED, "VERSION_CONFLICT")
        return ref.model_copy(update={"version": version, "encryption": "AES256"}), response

    def _read(self, ref: ArtifactObjectRef) -> tuple[bytes, dict[str, Any]]:
        self._metadata(ref)
        response = self._sdk("get_object", Key=ref.key)
        stream = response["Body"]
        try:
            body = stream.read(ref.size_bytes + 1)
        except BotoCoreError:
            raise failure(ProviderErrorCode.TEMPORARILY_UNAVAILABLE) from None
        finally:
            stream.close()
        if (
            len(body) != ref.size_bytes
            or hashlib.sha256(body).hexdigest() != ref.sha256
            or response.get("ServerSideEncryption") != "AES256"
        ):
            raise failure(ProviderErrorCode.VALIDATION_FAILED, "INTEGRITY_FAILED")
        return body, response

    async def put_private_artifact(
        self, ctx: StorageInvocationContext, command: ObjectWrite
    ) -> StorageResult[ArtifactObjectRef]:
        self._scope(ctx)
        if (
            isinstance(ctx, AuthorizedCaptureContext | EmailIngressContext)
            and command.artifact_id not in ctx.artifact_ids
        ):
            raise failure(ProviderErrorCode.VALIDATION_FAILED, "CAPTURE_SCOPE")
        if hashlib.sha256(command.body).hexdigest() != command.sha256 or len(command.body) > 10485760:
            raise failure(ProviderErrorCode.VALIDATION_FAILED, "INTEGRITY_FAILED")
        ref = ArtifactObjectRef(
            tenant_id=self.tenant_id,
            connection_id=self.connection_id,
            artifact_id=command.artifact_id,
            key=object_key(self.tenant_id, command.artifact_id, command.sha256),
            sha256=command.sha256,
            content_type=command.content_type,
            size_bytes=len(command.body),
            version=None,
            encryption="AES256",
        )

        def perform() -> StorageResult[ArtifactObjectRef]:
            encryption = self._sdk("get_bucket_encryption")
            rules = encryption.get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
            if not any(
                rule.get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm") == "AES256"
                for rule in rules
            ):
                raise failure(ProviderErrorCode.UNSUPPORTED_CAPABILITY, "ENCRYPTION_UNVERIFIED")
            try:
                existing, response = self._metadata(ref)
                self._read(existing)
                return self._result(ctx, existing, response)
            except StorageFailure as error:
                if error.reason != "OBJECT_MISSING":
                    raise
            self._sdk(
                "put_object",
                mutation=True,
                Key=ref.key,
                Body=command.body,
                ContentType=command.content_type,
                Metadata={"sha256": command.sha256},
                ServerSideEncryption="AES256",
            )
            stored, response = self._metadata(ref)
            self._read(stored)
            return self._result(ctx, stored, response)

        return await anyio.to_thread.run_sync(perform)

    async def get_metadata(
        self, ctx: StorageInvocationContext, ref: ArtifactObjectRef
    ) -> StorageResult[ArtifactObjectRef]:
        self._scope(ctx, ref)
        value, response = await anyio.to_thread.run_sync(partial(self._metadata, ref))
        return self._result(ctx, value, response)

    async def read_private_artifact(
        self, ctx: StorageInvocationContext, ref: ArtifactObjectRef
    ) -> StorageResult[bytes]:
        self._scope(ctx, ref)
        body, response = await anyio.to_thread.run_sync(partial(self._read, ref))
        return self._result(ctx, body, response)

    async def verify_integrity(
        self, ctx: StorageInvocationContext, ref: ArtifactObjectRef
    ) -> StorageResult[bool]:
        read = await self.read_private_artifact(ctx, ref)
        return StorageResult[bool](**{**read.model_dump(), "value": True})

    async def get_signed_read_url(
        self, ctx: StorageInvocationContext, ref: ArtifactObjectRef, ttl_seconds: int
    ) -> StorageResult[str]:
        if isinstance(ctx, AuthorizedCaptureContext | EmailIngressContext):
            raise failure(ProviderErrorCode.VALIDATION_FAILED, "CAPTURE_CANNOT_EXPORT")
        self._scope(ctx, ref)
        if not 1 <= ttl_seconds <= 60:
            raise failure(ProviderErrorCode.VALIDATION_FAILED, "EXPIRY_INVALID")
        url = await anyio.to_thread.run_sync(
            partial(
                self.client.generate_presigned_url,
                "get_object",
                Params={"Bucket": self.bucket, "Key": ref.key},
                ExpiresIn=ttl_seconds,
            )
        )
        return self._result(ctx, url, attempted=False)

    async def delete_object(
        self, ctx: StorageInvocationContext, ref: ArtifactObjectRef
    ) -> StorageResult[bool]:
        if isinstance(ctx, AuthorizedCaptureContext | EmailIngressContext):
            raise failure(ProviderErrorCode.VALIDATION_FAILED, "CAPTURE_CANNOT_DELETE")
        self._scope(ctx, ref, purge=True)

        def perform() -> StorageResult[bool]:
            response = self._sdk("delete_object", mutation=True, Key=ref.key)
            try:
                self._sdk("head_object", Key=ref.key)
            except StorageFailure as error:
                if error.reason == "OBJECT_MISSING":
                    return self._result(ctx, True, response)
                raise
            raise failure(ProviderErrorCode.UNKNOWN_SIDE_EFFECT, "DELETE_UNCONFIRMED", uncertain=True)

        return await anyio.to_thread.run_sync(perform)

    async def describe_capabilities(self) -> ProviderCapabilities:
        # Contract profile, not a probe or claim of a real provider operation.
        return ProviderCapabilities(
            features=frozenset(
                {
                    "private",
                    "encryption",
                    "expiry",
                    "provider_purge",
                    "integrity",
                    "authenticated_binary_read",
                }
            ),
            limits={"max_bytes": 10485760, "signed_read_seconds": 60, "versioning": 0},
        )
