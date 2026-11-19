"""Test-only local storage readiness for a freshly created synthetic browser workspace.

Invoke only through the explicit synthetic-reminders offline test profile. This does
not seed invoices, actions, observations, email or any business authority.
"""

import argparse
import asyncio
import hashlib
import json
import os
from ipaddress import IPv4Address
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.core.local_runtime import local_settings
from aco.core.types import Role
from aco.features.communications.models import Communication, EmailDispatchBinding
from aco.features.identity.models import LoginSession, ProviderConnection, Tenant, User, now
from aco.features.identity.service import IdentityService, Permission
from aco.features.usage.dto import ProviderUsageInput
from aco.features.usage.service import UsageService
from aco.integrations.demo_email.models import NativeEmailInvocation, NativeEmailOperation
from aco.integrations.ports.storage import ObjectWrite, StorageResult
from aco.integrations.s3.adapter import StorageRuntime


def require_local_profile(profile: str | None, endpoint: str | None, bucket: str | None) -> None:
    if profile != "synthetic-reminders":
        raise ValueError("Synthetic reminder browser profile required")
    parsed = urlsplit(endpoint or "")
    trusted = os.getenv("ACO_LOCAL_S3_IP")
    exact_local = False
    if trusted:
        address = IPv4Address(trusted)
        exact_local = (
            address.is_private
            and not address.is_loopback
            and not address.is_link_local
            and not address.is_multicast
            and not address.is_unspecified
            and (parsed.scheme, parsed.hostname, parsed.port) == ("http", str(address), 8333)
        )
    if (
        (parsed.scheme, parsed.hostname, parsed.port)
        not in {
            ("http", "127.0.0.1", 59000),
            ("http", "localhost", 59000),
            ("http", "storage", 8333),
        }
        and not exact_local
        or bucket != "aco-artifacts"
    ):
        raise ValueError("Configured local application storage only")


def current_owner(store: Session, workspace: UUID, owner: UUID) -> TenantContext:
    user = store.get(User, owner)
    if user is None or user.email.casefold() != "owner@example.test":
        raise ValueError("Actual test Owner required")
    session = store.scalar(
        select(LoginSession.id).where(
            LoginSession.user_id == owner,
            LoginSession.revoked.is_(False),
            LoginSession.expires_at > now(),
        )
    )
    if session is None:
        raise ValueError("Current Owner login required")
    ctx = TenantContext(tenant_id=workspace, actor_id=owner, role=Role.OWNER, correlation_id=uuid4())
    tenant = IdentityService(store).authorize(ctx, Permission.SETTINGS)
    if not isinstance(tenant, Tenant) or not tenant.is_demo:
        raise ValueError("Synthetic demo workspace required")
    return ctx


def record_probe(
    store: Session,
    ctx: TenantContext,
    connection: ProviderConnection,
    operation: str,
    artifact_id: UUID,
    result: StorageResult[Any],
) -> None:
    UsageService(store).record_usage(
        ctx,
        ProviderUsageInput(
            provider_connection_id=connection.id,
            operation_key=f"probe:{artifact_id}:{operation}",
            component="storage",
            operation=operation,
            provider_kind="storage",
            provider="s3",
            synthetic=True,
            cost=result.cost,
            native_operation_id=result.native_operation_id,
            outcome="VERIFIED",
        ),
    )


async def ensure_storage(workspace: UUID, owner: UUID) -> UUID:
    settings = local_settings()
    require_local_profile(os.getenv("ACO_TEST_PROFILE"), settings.storage_endpoint, settings.storage_bucket)
    if (
        settings.app_env != "local"
        or settings.external_providers != "fake"
        or settings.storage_access_key is None
        or settings.storage_secret_key is None
        or settings.database_url is None
        or settings.storage_endpoint is None
        or settings.storage_bucket is None
    ):
        raise ValueError("Local fake-provider application configuration required")
    engine = create_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)
    try:
        with Session(engine) as store:
            ctx = current_owner(store, workspace, owner)
            existing = list(
                store.scalars(
                    select(ProviderConnection).where(
                        ProviderConnection.tenant_id == workspace,
                        ProviderConnection.provider_type == "storage",
                    )
                )
            )
            if existing:
                if len(existing) == 1 and existing[0].provider == "s3" and existing[0].status == "READY":
                    return existing[0].id
                raise ValueError("Existing storage setup requires manual review")
            connection = ProviderConnection(
                tenant_id=workspace,
                provider_type="storage",
                provider="s3",
                status="NOT_CONFIGURED",
                external_account_id="synthetic-browser-local",
            )
            store.add(connection)
            store.flush()
            IdentityService(store).audit(ctx, "storage.synthetic_probe_started", connection.id)
            store.commit()
            runtime = StorageRuntime(
                endpoint=settings.storage_endpoint,
                bucket=settings.storage_bucket,
                access_key=settings.storage_access_key,
                secret_key=settings.storage_secret_key,
                local_endpoint_ip=settings.storage_local_endpoint_ip,
            )
            port = runtime.bind(workspace, connection.id)
            artifact_id = uuid4()
            body = b"aco synthetic storage probe " + artifact_id.bytes
            command = ObjectWrite(
                artifact_id=artifact_id,
                sha256=hashlib.sha256(body).hexdigest(),
                content_type="application/octet-stream",
                body=body,
            )
            written = await port.put_private_artifact(ctx, command)
            record_probe(store, ctx, connection, "put_private_artifact", artifact_id, written)
            ref = written.value
            try:
                verified = await port.verify_integrity(ctx, ref)
                if verified.value is not True:
                    raise ValueError("Storage integrity probe failed")
                record_probe(store, ctx, connection, "verify_integrity", artifact_id, verified)
                read = await port.read_private_artifact(ctx, ref)
                if read.value != body:
                    raise ValueError("Storage readback probe failed")
                record_probe(store, ctx, connection, "read_private_artifact", artifact_id, read)
            finally:
                deleted = await port.delete_object(ctx, ref)
                if deleted.value is not True:
                    raise ValueError("Storage probe deletion unverified")
                record_probe(store, ctx, connection, "delete_object", artifact_id, deleted)
            connection.status = "READY"
            connection.version += 1
            IdentityService(store).audit(ctx, "storage.synthetic_probe_ready", connection.id)
            store.commit()
            return connection.id
    finally:
        engine.dispose()


def read_evidence(workspace: UUID, owner: UUID, action: UUID) -> dict[str, int]:
    settings = local_settings()
    require_local_profile(os.getenv("ACO_TEST_PROFILE"), settings.storage_endpoint, settings.storage_bucket)
    if settings.database_url is None or settings.app_env != "local":
        raise ValueError("Local demo database required")
    engine = create_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)
    try:
        with Session(engine) as store:
            current_owner(store, workspace, owner)
            binding = store.scalar(
                select(EmailDispatchBinding).where(
                    EmailDispatchBinding.tenant_id == workspace,
                    EmailDispatchBinding.action_id == action,
                )
            )
            if binding is None:
                return {"native_operations": 0, "native_send_invocations": 0, "communications": 0}
            return {
                "native_operations": len(
                    list(
                        store.scalars(
                            select(NativeEmailOperation.id).where(
                                NativeEmailOperation.tenant_id == workspace,
                                NativeEmailOperation.operation_id == binding.operation_id,
                            )
                        )
                    )
                ),
                "native_send_invocations": len(
                    list(
                        store.scalars(
                            select(NativeEmailInvocation.id).where(
                                NativeEmailInvocation.tenant_id == workspace,
                                NativeEmailInvocation.operation_id == binding.operation_id,
                                NativeEmailInvocation.method == "send_email",
                            )
                        )
                    )
                ),
                "communications": len(
                    list(
                        store.scalars(
                            select(Communication.id).where(
                                Communication.tenant_id == workspace,
                                Communication.operation_id == binding.operation_id,
                            )
                        )
                    )
                ),
            }
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["ensure-storage", "evidence"])
    parser.add_argument("--workspace", required=True, type=UUID)
    parser.add_argument("--owner", required=True, type=UUID)
    parser.add_argument("--action", type=UUID)
    args = parser.parse_args()
    if args.command == "ensure-storage":
        connection_id = asyncio.run(ensure_storage(args.workspace, args.owner))
        print(json.dumps({"storage_connection_id": str(connection_id)}))
    else:
        if args.action is None:
            parser.error("--action is required for evidence")
        print(json.dumps(read_evidence(args.workspace, args.owner, args.action)))


if __name__ == "__main__":
    main()
