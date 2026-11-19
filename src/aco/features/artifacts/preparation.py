"""Current Owner prepares only the configured local APP store; never enables email ingress."""

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.core.types import CostQuality, ProviderCost
from aco.features.artifacts.consent import ConsentService
from aco.features.artifacts.consent_dto import PrepareConsentStorage, VoiceConsentPage
from aco.features.artifacts.storage_probe import probe_private_storage
from aco.features.identity.models import AuditEvent, ProviderConnection
from aco.features.identity.service import IdentityService, Permission
from aco.features.usage.dto import ProviderUsageInput
from aco.features.usage.models import UsageAttempt
from aco.features.usage.service import UsageService
from aco.integrations.ports.storage import StorageFailure, StorageResult, StorageRuntime

if TYPE_CHECKING:
    from aco.features.voice.runtime import VoiceRuntime


async def prepare_private_storage(
    voice: "VoiceRuntime", storage: StorageRuntime, ctx: TenantContext, command: PrepareConsentStorage
) -> VoiceConsentPage:
    def authorize(db: Session) -> None:
        service = ConsentService(db, voice, storage_available=True)
        tenant = service.human(ctx, Permission.SETTINGS)
        reasons = service.configuration_reasons(tenant)
        if reasons or storage.provider != "s3":
            raise HTTPException(403, reasons[0] if reasons else "APP_STORAGE_RUNTIME_REQUIRED")

    async with voice.sessions.begin() as db:
        await db.run_sync(authorize)
        rows = list(
            await db.scalars(
                select(ProviderConnection).where(
                    ProviderConnection.tenant_id == ctx.tenant_id,
                    ProviderConnection.provider_type == "storage",
                )
            )
        )
        if len(rows) > 1 or (rows and rows[0].provider != "s3"):
            raise HTTPException(409, "APP_STORAGE_CONNECTION_REVIEW_REQUIRED")
        old = await db.scalar(
            select(AuditEvent).where(
                AuditEvent.tenant_id == ctx.tenant_id,
                AuditEvent.action == "voice.storage_setup_requested",
                AuditEvent.target_id == command.request_id,
            )
        )
        if old and (old.actor_id != ctx.actor_id or old.details.get("command") != command.model_dump_json()):
            raise HTTPException(409, "APP_STORAGE_REQUEST_CONFLICT")
        marker = "synthetic-voice-storage:" + str(command.request_id)
        row = (
            rows[0]
            if rows
            else ProviderConnection(
                tenant_id=ctx.tenant_id,
                provider="s3",
                provider_type="storage",
                status="NOT_CONFIGURED",
                external_account_id=marker,
            )
        )
        if row.status != "READY" and (row.status != "NOT_CONFIGURED" or row.external_account_id != marker):
            raise HTTPException(409, "APP_STORAGE_CONNECTION_REVIEW_REQUIRED")
        if not rows:
            db.add(row)
            await db.flush()
        if old is None:
            await db.run_sync(
                lambda store: IdentityService(store).audit(
                    ctx,
                    "voice.storage_setup_requested",
                    command.request_id,
                    {"command": command.model_dump_json(), "connection_id": str(row.id)},
                )
            )
        key = row.id
        expected = row.version, row.status, row.external_account_id
        if row.status == "READY":
            return await db.run_sync(
                lambda store: ConsentService(store, voice, storage_available=True).list(ctx)
            )

    async def observe[T](
        operation: str, call: Callable[[], Awaitable[StorageResult[T]]], *, cleanup: bool = False
    ) -> StorageResult[T]:
        attempt_id = uuid4()
        async with voice.sessions.begin() as db:
            if not cleanup:
                await db.run_sync(authorize)
                current = await db.get(ProviderConnection, key, populate_existing=True)
                if (
                    current is None
                    or (current.version, current.status, current.external_account_id) != expected
                ):
                    raise HTTPException(409, "APP_STORAGE_CONNECTION_CHANGED")
            db.add(
                UsageAttempt(
                    id=attempt_id,
                    tenant_id=ctx.tenant_id,
                    provider_connection_id=key,
                    actor_id=ctx.actor_id,
                    correlation_id=ctx.correlation_id,
                    operation=operation,
                    outcome="PENDING",
                )
            )
        failure: Exception | None = None
        result: StorageResult[T]
        try:
            result = await call()
            cost, native, outcome = result.cost, result.native_operation_id, "VERIFIED"
        except Exception as error:
            failure = error
            cost, native = ProviderCost(amount=None, currency="USD", quality=CostQuality.UNKNOWN), None
            outcome = (
                "UNKNOWN_SIDE_EFFECT"
                if isinstance(error, StorageFailure) and error.failure.may_have_side_effect
                else "FAILED"
            )
        async with voice.sessions.begin() as db:
            event = ProviderUsageInput(
                provider_connection_id=key,
                operation_key="voice-storage:" + str(attempt_id),
                component="storage",
                operation=operation,
                provider_kind="storage",
                provider="s3",
                synthetic=True,
                cost=cost,
                native_operation_id=native,
                outcome=outcome,
            )
            recorded = await db.run_sync(lambda store: UsageService(store).record_verified(ctx, event))
            attempt = await db.get(UsageAttempt, attempt_id)
            assert attempt
            attempt.event_id, attempt.outcome, attempt.business_outcome = recorded.id, outcome, "OBSERVED"
        if failure:
            raise failure
        return result

    # Serialize exact setup work without blocking independently committed UsageAttempt rows.
    async with voice.sessions.begin() as db:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": "private-storage-setup:" + str(ctx.tenant_id)},
        )
        existing = await db.get(ProviderConnection, key, populate_existing=True)
        if existing is not None and existing.status == "READY":
            return await db.run_sync(
                lambda store: ConsentService(store, voice, storage_available=True).list(ctx)
            )
        port = storage.bind(ctx.tenant_id, key)
        if port.connection_id != key:
            raise HTTPException(403, "APP_STORAGE_BINDING_INVALID")
        try:
            capabilities = await probe_private_storage(port, ctx, command.request_id, observe)
        except Exception as error:
            raise HTTPException(503, "APP_STORAGE_PROBE_FAILED") from error
        await db.run_sync(authorize)
        published = await db.get(ProviderConnection, key, with_for_update=True, populate_existing=True)
        if (
            published is None
            or (published.version, published.status, published.external_account_id) != expected
        ):
            raise HTTPException(409, "APP_STORAGE_CONNECTION_CHANGED")
        published.status, published.capabilities, published.version = (
            "READY",
            capabilities,
            published.version + 1,
        )
        await db.run_sync(
            lambda store: IdentityService(store).audit(
                ctx,
                "voice.storage_setup_ready",
                command.request_id,
                {"connection_id": str(key), "synthetic": True},
            )
        )
    async with voice.sessions.begin() as db:
        return await db.run_sync(lambda store: ConsentService(store, voice, storage_available=True).list(ctx))
