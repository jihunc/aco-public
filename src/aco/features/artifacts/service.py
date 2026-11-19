"""Durable private originals, fresh membership and independently recorded SDK attempts."""

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from aco.features.communications.capture import CaptureInvocation
    from aco.features.communications.email_live import EmailLiveAuthorization
    from aco.features.communications.provider_ingress import ProviderIngressRuntime
    from aco.features.communications.provider_outbound import ProviderOutboundRuntime
    from aco.features.voice.runtime import VoiceRuntime
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from aco.core.context import ActorContext, TenantContext
from aco.core.types import CostQuality, ProviderCost, ProviderErrorCode, ProviderFailure
from aco.features.artifacts.dto import ArtifactRef, ArtifactWrite, AuthorizedDownload, DeletionReceipt
from aco.features.artifacts.models import Artifact, ArtifactPurge, RetentionPolicy
from aco.features.identity.models import ProviderConnection, Tenant, now
from aco.features.identity.service import IdentityService, Permission
from aco.features.usage.dto import ProviderUsageInput
from aco.features.usage.models import UsageAttempt
from aco.features.usage.service import UsageService
from aco.integrations.ports.email_context import EmailIngressContext
from aco.integrations.ports.purge_context import ArtifactPurgeContext
from aco.integrations.ports.storage import (
    ArtifactObjectRef,
    ObjectWrite,
    StorageFailure,
    StorageInvocationContext,
    StoragePort,
    StorageResult,
    StorageRuntime,
)
from aco.integrations.ports.voice_artifacts import VoiceArtifactPort, VoiceArtifactRequest

T = TypeVar("T")
EMAIL_TYPES = {"message/rfc822", "text/plain", "text/html", "application/pdf", "image/png", "image/jpeg"}
VOICE_TYPES = {"audio/wav", "audio/mpeg", "application/json"}


def artifact_view(row: Artifact) -> ArtifactRef:
    return ArtifactRef(**{key: getattr(row, key) for key in ArtifactRef.model_fields})


def object_ref(row: Artifact) -> ArtifactObjectRef:
    return ArtifactObjectRef(
        tenant_id=row.tenant_id,
        connection_id=row.provider_connection_id,
        artifact_id=row.id,
        key=row.object_key,
        sha256=row.sha256,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        version=row.object_version,
        encryption=row.encryption or "AES256",
    )


class ArtifactService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        runtime: StorageRuntime,
        app_env: str,
        *,
        voice_artifacts: VoiceArtifactPort | None = None,
    ):
        self.sessions, self.runtime, self.app_env = sessions, runtime, app_env
        self.voice_artifacts = voice_artifacts

    def _authorize(
        self, db: Session, ctx: ActorContext | ArtifactPurgeContext, permission: Permission
    ) -> Any:
        if isinstance(ctx, ArtifactPurgeContext):
            if permission != Permission.DELETE_AUTHORIZE:
                raise HTTPException(403, "PURGE_ONLY_CONTEXT")
            from aco.features.artifacts.retention import current_purge

            return current_purge(db, ctx)
        if not isinstance(ctx, TenantContext):
            raise HTTPException(403, "HUMAN_STORAGE_CONTEXT_REQUIRED")
        return IdentityService(db).authorize(ctx, permission)

    async def _audit(
        self,
        db: AsyncSession,
        ctx: StorageInvocationContext,
        action: str,
        target: UUID,
        details: dict[str, str | bool] | None = None,
    ) -> None:
        await db.run_sync(lambda store: IdentityService(store).audit(ctx, action, target, details))

    async def _row(self, db: AsyncSession, ctx: StorageInvocationContext, artifact_id: UUID) -> Artifact:
        row = await db.scalar(
            select(Artifact)
            .where(Artifact.tenant_id == ctx.tenant_id, Artifact.id == artifact_id)
            .with_for_update()
        )
        if row is None:
            raise HTTPException(404, "ARTIFACT_NOT_FOUND")
        return row

    async def _port(
        self, db: AsyncSession, ctx: StorageInvocationContext, connection_id: UUID | None = None
    ) -> StoragePort:
        query = select(ProviderConnection).where(
            ProviderConnection.tenant_id == ctx.tenant_id,
            ProviderConnection.provider_type == "storage",
            ProviderConnection.status == "READY",
        )
        if connection_id is not None:
            query = query.where(ProviderConnection.id == connection_id)
        rows = list((await db.scalars(query)).all())
        if len(rows) != 1 or rows[0].provider != self.runtime.provider:
            raise HTTPException(503, "STORAGE_CONNECTION_UNAVAILABLE")
        port = self.runtime.bind(ctx.tenant_id, rows[0].id)
        if (port.provider, port.tenant_id, port.connection_id) != (
            rows[0].provider,
            ctx.tenant_id,
            rows[0].id,
        ):
            raise HTTPException(503, "STORAGE_BINDING_INVALID")
        return port

    async def _observe(
        self,
        db: AsyncSession,
        ctx: StorageInvocationContext,
        port: StoragePort,
        operation: str,
        call: Callable[[], Awaitable[StorageResult[T]]],
        *,
        before_call: Callable[[], Awaitable[None]] | None = None,
    ) -> StorageResult[T]:
        key = uuid4()
        async with self.sessions.begin() as evidence:
            await evidence.execute(text("SET LOCAL lock_timeout = '2s'"))
            evidence.add(
                UsageAttempt(
                    id=key,
                    tenant_id=ctx.tenant_id,
                    provider_connection_id=port.connection_id,
                    actor_id=ctx.actor_id,
                    correlation_id=ctx.correlation_id,
                    operation=operation,
                    outcome="PENDING",
                )
            )
        if before_call is not None:
            try:
                await before_call()
            except Exception:
                # Intent exists, but the SDK was never called: no provider charge/event is invented.
                async with self.sessions.begin() as evidence:
                    attempt = await evidence.get(UsageAttempt, key, with_for_update=True)
                    assert attempt is not None
                    attempt.outcome, attempt.business_outcome = "NO_EFFECT", "DENIED"
                    attempt.error_reference = "PRE_WIRE_AUTHORITY_DENIED"
                raise
        transaction = db.sync_session.get_nested_transaction() or db.sync_session.get_transaction()
        assert transaction is not None
        db.info.setdefault("usage_attempt_ids", {}).setdefault(transaction, []).append(key)
        try:
            result = await call()
            if operation == "delete_object" and result.value is not True:
                raise StorageFailure(
                    ProviderFailure(
                        code=ProviderErrorCode.UNKNOWN_SIDE_EFFECT,
                        retryable=False,
                        may_have_side_effect=True,
                        request_id=result.native_operation_id,
                    ),
                    "DELETE_UNCONFIRMED",
                )
            if (result.tenant_id, result.connection_id, result.provider, result.correlation_id) != (
                ctx.tenant_id,
                port.connection_id,
                port.provider,
                ctx.correlation_id,
            ):
                raise HTTPException(503, "STORAGE_PROVENANCE_INVALID")
        except Exception as error:
            failure = error.failure if isinstance(error, StorageFailure) else None
            await self._finish(
                ctx,
                port,
                key,
                operation,
                failure.code.value if failure else "UNKNOWN_SIDE_EFFECT",
                failure.request_id if failure else None,
            )
            raise
        await self._finish(ctx, port, key, operation, "VERIFIED", result.native_operation_id, result)
        return result

    async def _finish(
        self,
        ctx: StorageInvocationContext,
        port: StoragePort,
        key: UUID,
        operation: str,
        outcome: str,
        native: str | None,
        result: StorageResult[Any] | None = None,
    ) -> None:
        async with self.sessions.begin() as evidence:
            attempt = await evidence.get(UsageAttempt, key, with_for_update=True)
            assert attempt is not None
            value = ProviderUsageInput(
                provider_connection_id=port.connection_id,
                operation_key="native:" + native if native else "attempt:" + str(key),
                component="storage",
                operation=operation,
                provider_kind="storage",
                provider=port.provider,
                synthetic=True,
                cost=result.cost
                if result
                else ProviderCost(amount=None, currency="USD", quality=CostQuality.UNKNOWN),
                native_operation_id=native,
                outcome=outcome,
            )
            event = await evidence.run_sync(lambda store: UsageService(store).record_verified(ctx, value))
            attempt.event_id, attempt.outcome, attempt.error_reference = (
                event.id,
                outcome,
                native if outcome != "VERIFIED" else None,
            )
            # Provider observation is durable; artifact transaction may still fail.
            attempt.business_outcome = "PENDING"

    async def store_artifact(self, ctx: ActorContext, command: ArtifactWrite) -> ArtifactRef:
        if command.capture_class == "synthetic_consented_voice":
            raise HTTPException(403, "VOICE_SESSION_CAPTURE_REQUIRED")
        return await self._store_artifact(
            ctx, command, lambda store: self._authorize(store, ctx, Permission.SETTINGS)
        )

    async def store_voice_artifact(
        self,
        ctx: TenantContext,
        request: "VoiceArtifactRequest",
        command: ArtifactWrite,
        native: str,
        runtime: "VoiceRuntime",
    ) -> ArtifactRef:
        from uuid import uuid5

        from aco.features.artifacts.consent_models import ConsentEvidence, VoiceArtifactLink
        from aco.features.voice.postcall import permitted

        if (
            command.artifact_id != uuid5(request.session_id, "voice-private:" + request.kind)
            or command.source_kind != "provider_call"
            or command.capture_class != "synthetic_consented_voice"
            or command.source_reference != "synthetic-call:" + str(request.session_id) + ":" + request.kind
            or command.captured_at != request.captured_at
            or command.content_type
            not in ({"application/json"} if request.kind == "transcript" else {"audio/wav", "audio/mpeg"})
        ):
            raise HTTPException(403, "VOICE_CAPTURE_IDENTITY_DENIED")
        async with self.sessions.begin() as db:
            await db.run_sync(lambda store: permitted(store, ctx, request, runtime))
            consent = await db.get(ConsentEvidence, request.consent_id)
            assert consent and consent.storage_connection_id
            storage_id = consent.storage_connection_id

        def link(store: Session, row: Artifact) -> None:
            permitted(store, ctx, request, runtime)
            old = store.scalar(select(VoiceArtifactLink).where(VoiceArtifactLink.artifact_id == row.id))
            if old is not None:
                if (old.tenant_id, old.session_id, old.consent_id, old.kind, old.native_reference) != (
                    ctx.tenant_id,
                    request.session_id,
                    request.consent_id,
                    request.kind,
                    native,
                ):
                    raise HTTPException(409, "VOICE_CAPTURE_IDENTITY_CONFLICT")
                return
            store.add(
                VoiceArtifactLink(
                    tenant_id=ctx.tenant_id,
                    session_id=request.session_id,
                    consent_id=request.consent_id,
                    artifact_id=row.id,
                    kind=request.kind,
                    native_reference=native,
                )
            )
            store.flush()
            consent = store.get(ConsentEvidence, request.consent_id)
            assert consent
            row.expires_at = min(row.expires_at, consent.expires_at)
            purge = store.scalar(select(ArtifactPurge).where(ArtifactPurge.artifact_id == row.id))
            assert purge
            purge.due_at = min(purge.due_at, row.expires_at)

        return await self._store_artifact(
            ctx,
            command,
            lambda store: permitted(store, ctx, request, runtime),
            before_write=True,
            storage_id=storage_id,
            on_identity=link,
        )

    async def store_ingested_artifact(
        self,
        invocation: "CaptureInvocation",
        grant_id: UUID,
        command: ArtifactWrite,
        *,
        provider_outbound: "ProviderOutboundRuntime | None" = None,
    ) -> ArtifactRef:
        from aco.features.communications.capture import validate_capture

        return await self._store_artifact(
            invocation.context,
            command,
            lambda store: validate_capture(
                store, invocation, grant_id, command, provider_outbound=provider_outbound
            ),
            before_write=provider_outbound is not None,
            approved_email=provider_outbound.live_authorization if provider_outbound is not None else None,
        )

    async def store_inbound_artifact(
        self,
        ctx: EmailIngressContext,
        command: ArtifactWrite,
        *,
        provider_ingress: "ProviderIngressRuntime | None" = None,
    ) -> ArtifactRef:
        from aco.features.communications.inbound import validate_capture

        return await self._store_artifact(
            ctx,
            command,
            lambda store: validate_capture(store, ctx, command, provider_ingress=provider_ingress),
            before_write=True,
            approved_email=provider_ingress.live_authorization if provider_ingress is not None else None,
        )

    async def read_ingested_attachment(
        self, invocation: "CaptureInvocation", grant_id: UUID, artifact_id: UUID
    ) -> AuthorizedDownload:
        from aco.features.communications.capture import validate_capture

        ctx = invocation.context
        async with self.sessions.begin() as db:
            await db.run_sync(
                lambda store: validate_capture(store, invocation, grant_id, artifact_id=artifact_id)
            )
            row = await self._row(db, ctx, artifact_id)
            if (
                row.storage_state != "stored"
                or row.revoked_at
                or row.expires_at <= now()
                or row.consent_revoked
                or row.legal_hold_state != "none"
            ):
                raise HTTPException(410, "ARTIFACT_UNAVAILABLE")
            port = await self._port(db, ctx, row.provider_connection_id)
            result = await self._observe(
                db,
                ctx,
                port,
                "read_private_artifact",
                lambda: port.read_private_artifact(ctx, object_ref(row)),
            )
            await self._audit(db, ctx, "artifact.capture_attachment_read", row.id)
            return AuthorizedDownload(
                artifact_id=row.id, body=result.value, content_type=row.content_type, sha256=row.sha256
            )

    async def _store_artifact(
        self,
        ctx: StorageInvocationContext,
        command: ArtifactWrite,
        authorize: Callable[[Session], Tenant],
        *,
        before_write: bool = False,
        approved_email: "EmailLiveAuthorization | None" = None,
        storage_id: UUID | None = None,
        on_identity: Callable[[Session, Artifact], None] | None = None,
    ) -> ArtifactRef:
        from aco.features.communications.email_live import ApprovedEmailRetention

        base_authorize = authorize

        def authorize_current(store: Session) -> Tenant:
            tenant = base_authorize(store)
            if command.capture_class == "real":
                storage_id = getattr(ctx, "storage_connection_id", None)
                if approved_email is None or not isinstance(storage_id, UUID):
                    raise HTTPException(403, "APPROVED_CAPTURE_POLICY_REQUIRED")
                approved_email.retention(store, ctx.tenant_id, storage_id)
            return tenant

        authorize = authorize_current
        digest = hashlib.sha256(command.body).hexdigest()
        async with self.sessions.begin() as db:
            tenant = await db.run_sync(authorize)
            if self.app_env not in {"local", "demo"} or not tenant.is_demo:
                raise HTTPException(403, "APPROVED_CAPTURE_POLICY_REQUIRED")
            port = await self._port(db, ctx, storage_id or getattr(ctx, "storage_connection_id", None))
            approved_policy = None
            approval = None
            if command.capture_class == "real":
                assert approved_email is not None
                approved_policy = await db.run_sync(
                    lambda store: approved_email.retention(store, ctx.tenant_id, port.connection_id)
                )
                approval = ApprovedEmailRetention.model_validate_json(json.dumps(approved_policy.approval))
            row = await db.scalar(
                select(Artifact)
                .where(
                    Artifact.tenant_id == ctx.tenant_id,
                    Artifact.source_kind == command.source_kind,
                    Artifact.source_reference == command.source_reference,
                )
                .with_for_update()
            )
            if row is not None:
                if approved_policy is not None and row.retention_policy_id != approved_policy.id:
                    raise HTTPException(403, "APPROVED_CAPTURE_POLICY_REQUIRED")
                if (row.sha256, row.content_type, row.capture_class) != (
                    digest,
                    command.content_type,
                    command.capture_class,
                ):
                    raise HTTPException(409, "ORIGINAL_IDENTITY_CONFLICT")
                if (
                    row.storage_state in {"deleted", "purge_pending", "blocked"}
                    or row.expires_at <= now()
                    or row.consent_revoked
                ):
                    raise HTTPException(410, "ARTIFACT_REVOKED")
                if row.storage_state in {"stored", "quarantined"}:
                    return artifact_view(row)
            else:
                captured = command.captured_at or now()
                if captured > now():
                    raise HTTPException(422, "CAPTURE_TIME_IN_FUTURE")
                policy = approved_policy or await db.scalar(
                    select(RetentionPolicy).where(
                        RetentionPolicy.tenant_id == ctx.tenant_id,
                        RetentionPolicy.policy_key == "synthetic_30_days",
                    )
                )
                if policy is None:
                    policy = RetentionPolicy(
                        tenant_id=ctx.tenant_id,
                        policy_key="synthetic_30_days",
                        synthetic=True,
                        retention_days=30,
                        authority="synthetic_only_no_real_capture_authority",
                    )
                    db.add(policy)
                    await db.flush()
                allowed = EMAIL_TYPES if command.capture_class in {"synthetic_email", "real"} else VOICE_TYPES
                valid = (
                    len(command.body) <= (approval.max_bytes if approval is not None else 10485760)
                    and command.content_type in allowed
                )
                expires_at = captured + timedelta(days=policy.retention_days)
                if approval is not None:
                    expires_at = min(expires_at, approval.effective_until)
                key = command.artifact_id or uuid4()
                # Provider-neutral canonical key is defined by the artifact identity contract.
                row = Artifact(
                    id=key,
                    tenant_id=ctx.tenant_id,
                    provider_connection_id=port.connection_id,
                    retention_policy_id=policy.id,
                    source_kind=command.source_kind,
                    source_reference=command.source_reference,
                    sha256=digest,
                    content_type=command.content_type,
                    size_bytes=len(command.body),
                    capture_class=command.capture_class,
                    captured_at=captured,
                    expires_at=expires_at,
                    storage_state="pending" if valid else "quarantined",
                    object_key=f"tenants/{ctx.tenant_id}/artifacts/{key}/{digest}",
                    failure_code=None if valid else "MEDIA_REJECTED",
                )
                db.add(row)
                await db.flush()
                db.add(
                    ArtifactPurge(
                        tenant_id=ctx.tenant_id,
                        artifact_id=row.id,
                        due_at=row.expires_at,
                        status="scheduled",
                        reason="retention_expiry",
                    )
                )
                await self._audit(
                    db, ctx, "artifact.capture_intent" if valid else "artifact.quarantined", row.id
                )
            if on_identity is not None:
                assert row is not None
                identity_row = row
                await db.run_sync(lambda store: on_identity(store, identity_row))
            artifact_id = row.id
            if row.storage_state == "quarantined":
                return artifact_view(row)
        # The original's identity and purge request have committed before SDK work.
        try:
            async with self.sessions.begin() as db:
                await db.run_sync(authorize)
                row = await self._row(db, ctx, artifact_id)
                if row.storage_state == "stored":
                    return artifact_view(row)
                if (
                    row.revoked_at
                    or row.expires_at <= now()
                    or row.consent_revoked
                    or row.legal_hold_state != "none"
                ):
                    raise HTTPException(410, "ARTIFACT_REVOKED")
                port = await self._port(db, ctx, row.provider_connection_id)
                write = ObjectWrite(
                    artifact_id=row.id, sha256=row.sha256, content_type=row.content_type, body=command.body
                )

                async def authorize_write() -> None:
                    await db.run_sync(authorize)

                result = await self._observe(
                    db,
                    ctx,
                    port,
                    "put_private_artifact",
                    lambda: port.put_private_artifact(ctx, write),
                    before_call=authorize_write if before_write else None,
                )
                row.object_version, row.encryption = result.value.version, result.value.encryption
                row.storage_state, row.failure_code = "stored", None
                await self._audit(db, ctx, "artifact.stored", row.id)
                value = artifact_view(row)
            return value
        except StorageFailure as error:
            async with self.sessions.begin() as db:
                row = await self._row(db, ctx, artifact_id)
                if row.storage_state == "pending":
                    row.storage_state, row.failure_code = "failed", error.failure.code.value
            raise

    async def _revoke(
        self, db: AsyncSession, ctx: ActorContext | ArtifactPurgeContext, row: Artifact, reason: str
    ) -> ArtifactPurge:
        row.revoked_at = row.revoked_at or now()
        row.storage_state = (
            "blocked" if row.legal_hold_state != "none" or row.storage_state == "blocked" else "purge_pending"
        )
        job = await db.scalar(
            select(ArtifactPurge).where(ArtifactPurge.artifact_id == row.id).with_for_update()
        )
        assert job is not None
        job.due_at, job.reason = min(job.due_at, now()), reason
        # Denied reads reassert access withdrawal, not worker cancellation. Preserve the
        # persisted job/lease until its executor or canonical recovery settles it.
        if job.status not in {"queued", "executing", "deleted"}:
            job.status = row.storage_state
        if isinstance(ctx, ArtifactPurgeContext):
            job.status = "executing"
        await self._audit(db, ctx, "artifact.access_revoked", row.id, {"reason": reason})
        return job

    async def describe(self, ctx: ActorContext, artifact_id: UUID) -> ArtifactRef:
        async with self.sessions.begin() as db:
            await db.run_sync(lambda store: self._authorize(store, ctx, Permission.INVOICE_READ))
            row = await self._row(db, ctx, artifact_id)
            if row.storage_state != "deleted" and (
                row.expires_at <= now() or row.consent_revoked or row.legal_hold_state != "none"
            ):
                await self._revoke(db, ctx, row, "retention_or_consent")
            return artifact_view(row)

    async def authorize_artifact_read(self, ctx: ActorContext, artifact_id: UUID) -> AuthorizedDownload:
        denied = False
        async with self.sessions.begin() as db:
            await db.run_sync(lambda store: self._authorize(store, ctx, Permission.INVOICE_READ))
            row = await self._row(db, ctx, artifact_id)
            from aco.features.artifacts.consent import voice_read_allowed

            voice_allowed = await db.run_sync(lambda store: voice_read_allowed(store, row))
            if (
                row.storage_state == "deleted"
                or row.revoked_at
                or row.expires_at <= now()
                or row.consent_revoked
                or row.legal_hold_state != "none"
            ):
                if row.storage_state != "deleted":
                    await self._revoke(db, ctx, row, "retention_or_consent")
                denied = True
            elif not voice_allowed or row.storage_state != "stored":
                denied = True
            else:
                port = await self._port(db, ctx, row.provider_connection_id)
                result = await self._observe(
                    db,
                    ctx,
                    port,
                    "read_private_artifact",
                    lambda: port.read_private_artifact(ctx, object_ref(row)),
                )
                await self._audit(db, ctx, "artifact.read", row.id)
                value = AuthorizedDownload(
                    artifact_id=row.id, body=result.value, content_type=row.content_type, sha256=row.sha256
                )
        if denied:
            raise HTTPException(410, "ARTIFACT_UNAVAILABLE")
        return value

    async def delete_artifact(self, ctx: ActorContext, artifact_id: UUID, reason: str) -> DeletionReceipt:
        blocked = False
        async with self.sessions.begin() as db:
            await db.run_sync(lambda store: self._authorize(store, ctx, Permission.DELETE_AUTHORIZE))
            row = await self._row(db, ctx, artifact_id)
            if row.storage_state != "deleted":
                await self._revoke(db, ctx, row, "owner_deletion")
            blocked = row.legal_hold_state != "none"
        if blocked:
            raise HTTPException(409, "RETENTION_REVIEW_REQUIRED")
        return await self.execute_purge(ctx, artifact_id)

    async def execute_purge(
        self, ctx: ActorContext | ArtifactPurgeContext, artifact_id: UUID
    ) -> DeletionReceipt:
        if isinstance(ctx, ArtifactPurgeContext) and artifact_id != ctx.artifact_id:
            raise HTTPException(403, "PURGE_ONLY_CONTEXT")
        async with self.sessions.begin() as db:
            await db.run_sync(lambda store: self._authorize(store, ctx, Permission.DELETE_AUTHORIZE))
            row = await self._row(db, ctx, artifact_id)
            job = await db.scalar(
                select(ArtifactPurge)
                .where(ArtifactPurge.tenant_id == ctx.tenant_id, ArtifactPurge.artifact_id == row.id)
                .with_for_update()
            )
            assert job is not None
            if row.storage_state != "deleted":
                if row.legal_hold_state != "none" or (row.revoked_at is None and row.expires_at > now()):
                    raise HTTPException(409, "PURGE_NOT_AUTHORIZED")
                await self._revoke(db, ctx, row, job.reason)
                port = await self._port(db, ctx, row.provider_connection_id)
                try:

                    async def authorize_delete() -> None:
                        await db.run_sync(
                            lambda store: self._authorize(store, ctx, Permission.DELETE_AUTHORIZE)
                        )

                    result = await self._observe(
                        db,
                        ctx,
                        port,
                        "delete_object",
                        lambda: port.delete_object(ctx, object_ref(row)),
                        before_call=authorize_delete,
                    )
                except StorageFailure as error:
                    row.storage_state, job.status = "blocked", "blocked"
                    row.failure_code = job.failure_code = error.failure.code.value
                    job.due_at = now() + timedelta(minutes=1)
                else:
                    row.deleted_at = now()
                    job.provider_receipt = result.native_operation_id
                    provider_deleted = await self._purge_voice_provider(db, ctx, row)
                    row.storage_state, job.status = (
                        ("deleted", "deleted") if provider_deleted else ("blocked", "blocked")
                    )
                    job.confirmed_at = now() if provider_deleted else None
                    if not provider_deleted:
                        row.failure_code = job.failure_code = "VOICE_PROVIDER_PURGE_UNAVAILABLE"
                    else:
                        row.failure_code = job.failure_code = None
                    if provider_deleted:
                        await self._audit(db, ctx, "artifact.purge_confirmed", row.id)
            return DeletionReceipt(
                artifact_id=row.id,
                purge_id=job.id,
                status=job.status,
                sha256=row.sha256,
                confirmed_at=job.confirmed_at,
                provider_receipt=job.provider_receipt,
                failure_code=job.failure_code,
            )

    async def _purge_voice_provider(
        self, db: AsyncSession, ctx: ActorContext | ArtifactPurgeContext, row: Artifact
    ) -> bool:
        from aco.features.artifacts.consent_models import VoiceArtifactLink
        from aco.features.voice.models import VoiceSession

        link = await db.scalar(
            select(VoiceArtifactLink).where(
                VoiceArtifactLink.tenant_id == row.tenant_id, VoiceArtifactLink.artifact_id == row.id
            )
        )
        if row.capture_class != "synthetic_consented_voice":
            return True
        if link is None:
            return False
        if link.provider_purged_at and link.provider_purge_receipt:
            return True
        if self.voice_artifacts is None:
            return False
        session = await db.get(VoiceSession, link.session_id)
        if session is None or session.tenant_id != row.tenant_id or session.provider_call_id is None:
            return False
        from typing import Literal

        kind: Literal["transcript", "recording"]
        if link.kind == "transcript":
            kind = "transcript"
        elif link.kind == "recording":
            kind = "recording"
        else:
            return False
        request = VoiceArtifactRequest(
            tenant_id=row.tenant_id,
            connection_id=session.connection_id,
            session_id=session.id,
            consent_id=link.consent_id,
            provider_call_id=session.provider_call_id,
            captured_at=row.captured_at,
            kind=kind,
        )

        async def authorize(received: VoiceArtifactRequest) -> None:
            if received != request:
                raise HTTPException(403, "VOICE_PURGE_SCOPE_DENIED")
            await db.run_sync(lambda store: self._authorize(store, ctx, Permission.DELETE_AUTHORIZE))

        receipt = await self.voice_artifacts.purge(request, authorize)
        link.provider_purged_at, link.provider_purge_receipt = now(), receipt
        return True
