"""Current Owner preparation through configured local APP ports; readiness follows actual probes."""

from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.core.types import CostQuality, ProviderCost
from aco.features.communications.ingress import EmailIngressService
from aco.features.communications.ingress_dto import IngressSetupView, PrepareIngress
from aco.features.communications.simulation import setup
from aco.features.identity.models import AuditEvent, ProviderConnection
from aco.features.identity.service import IdentityService, Permission
from aco.features.usage.dto import ProviderUsageInput
from aco.features.usage.models import UsageAttempt
from aco.features.usage.service import UsageService
from aco.integrations.demo_email.adapter import DemoEmailAdapter
from aco.integrations.ports.storage import StorageFailure, StorageResult


def authorize(db: Session, ctx: TenantContext, service: EmailIngressService) -> None:
    if not isinstance(ctx, TenantContext):
        raise HTTPException(403, "HUMAN_REQUIRED")
    tenant = IdentityService(db).authorize(ctx, Permission.SETTINGS)
    if (
        not tenant.is_demo
        or service.environment != "local"
        or not service.enabled
        or not service.default_secret
        or service.storage_runtime is None
        or service.storage_runtime.provider != "s3"
    ):
        raise HTTPException(403, "SYNTHETIC_INBOX_PREPARATION_DISABLED")


def _reserve(
    db: Session, ctx: TenantContext, service: EmailIngressService, command: PrepareIngress
) -> tuple[UUID, UUID]:
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
        {"key": f"ingress-setup:{ctx.tenant_id}"},
    )
    authorize(db, ctx, service)
    old = db.scalar(
        select(AuditEvent).where(
            AuditEvent.tenant_id == ctx.tenant_id,
            AuditEvent.action == "email.ingress_setup_requested",
            AuditEvent.target_id == command.request_id,
        )
    )
    if old is not None and (
        old.actor_id != ctx.actor_id or old.details.get("command") != command.model_dump_json()
    ):
        raise HTTPException(409, "INGRESS_SETUP_REQUEST_CONFLICT")
    rows = list(db.scalars(select(ProviderConnection).where(ProviderConnection.tenant_id == ctx.tenant_id)))
    chosen = []
    for kind, provider in (("email", "demo_email"), ("storage", "s3")):
        matches = [
            row
            for row in rows
            if row.provider_type == kind and (kind == "storage" or row.provider == provider)
        ]
        if len(matches) > 1:
            raise HTTPException(409, "INGRESS_SETUP_CONNECTION_REVIEW_REQUIRED")
        marker = f"synthetic-ingress-setup:{command.request_id}"
        if matches:
            row = matches[0]
            if row.provider != provider or (
                row.status != "READY"
                and (row.status != "NOT_CONFIGURED" or row.external_account_id != marker)
            ):
                raise HTTPException(409, "INGRESS_SETUP_CONNECTION_REVIEW_REQUIRED")
        else:
            row = ProviderConnection(
                tenant_id=ctx.tenant_id,
                provider_type=kind,
                provider=provider,
                status="NOT_CONFIGURED",
                external_account_id=marker,
            )
            db.add(row)
            db.flush()
        chosen.append(row.id)
    email, storage = chosen
    details: dict[str, str | bool] = {
        "command": command.model_dump_json(),
        "email_connection_id": str(email),
        "storage_connection_id": str(storage),
        "synthetic": True,
    }
    if old is not None:
        if old.details != details:
            raise HTTPException(409, "INGRESS_SETUP_REQUEST_CONFLICT")
    else:
        IdentityService(db).audit(ctx, "email.ingress_setup_requested", command.request_id, details)
    return email, storage


async def _observe[T](
    service: EmailIngressService,
    ctx: TenantContext,
    connection: ProviderConnection,
    operation: str,
    call: Callable[[], Awaitable[T]],
    metadata: Callable[[T], tuple[ProviderCost, str | None]],
    *,
    cleanup: bool = False,
) -> T:
    key = uuid4()
    async with service.sessions.begin() as db:
        if not cleanup:
            await db.run_sync(lambda store: authorize(store, ctx, service))
        db.add(
            UsageAttempt(
                id=key,
                tenant_id=ctx.tenant_id,
                provider_connection_id=connection.id,
                actor_id=ctx.actor_id,
                correlation_id=ctx.correlation_id,
                operation=operation,
                outcome="PENDING",
            )
        )
    failure: Exception | None = None
    result: T
    try:
        result = await call()
        cost, native = metadata(result)
        outcome = "VERIFIED"
    except Exception as error:
        failure = error
        cost, native = ProviderCost(amount=None, currency="USD", quality=CostQuality.UNKNOWN), None
        outcome = (
            "UNKNOWN_SIDE_EFFECT"
            if isinstance(error, StorageFailure) and error.failure.may_have_side_effect
            else "FAILED"
        )
    async with service.sessions.begin() as db:
        event = ProviderUsageInput(
            provider_connection_id=connection.id,
            operation_key=f"ingress-setup:{key}:{operation}",
            component=connection.provider_type,
            operation=operation,
            provider_kind=connection.provider_type,
            provider=connection.provider,
            synthetic=True,
            cost=cost,
            native_operation_id=native,
            outcome=outcome,
        )
        recorded = await db.run_sync(lambda store: UsageService(store).record_verified(ctx, event))
        attempt = await db.get(UsageAttempt, key)
        assert attempt is not None
        attempt.outcome, attempt.event_id = outcome, recorded.id
        attempt.business_outcome = "OBSERVED"
    if failure is not None:
        raise failure
    return result


async def prepare(
    service: EmailIngressService, ctx: TenantContext, command: PrepareIngress
) -> IngressSetupView:
    async with service.sessions.begin() as db:
        email_id, storage_id = await db.run_sync(lambda store: _reserve(store, ctx, service, command))
    failed: Exception | None = None
    async with service.sessions.begin() as db:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"ingress-setup:{ctx.tenant_id}"},
        )
        rows = list(
            await db.scalars(
                select(ProviderConnection)
                .where(
                    ProviderConnection.tenant_id == ctx.tenant_id,
                    ProviderConnection.id.in_([email_id, storage_id]),
                )
                .order_by(ProviderConnection.id)
                .execution_options(populate_existing=True)
            )
        )
        email = next(row for row in rows if row.id == email_id)
        storage = next(row for row in rows if row.id == storage_id)
        expected = {row.id: (row.version, row.status, row.provider, row.external_account_id) for row in rows}
        verified_capabilities: dict[UUID, list[str]] = {}
        try:
            if storage.status != "READY":
                if storage.status != "NOT_CONFIGURED" or storage.provider != "s3":
                    raise HTTPException(409, "INGRESS_SETUP_CONNECTION_CHANGED")
                assert service.storage_runtime is not None
                port = service.storage_runtime.bind(ctx.tenant_id, storage.id)
                if (port.tenant_id, port.connection_id, port.provider) != (ctx.tenant_id, storage.id, "s3"):
                    raise HTTPException(503, "INGRESS_SETUP_STORAGE_BINDING")
                from aco.features.artifacts.storage_probe import probe_private_storage

                async def observe_probe[T](
                    operation: str, call: Callable[[], Awaitable[StorageResult[T]]], *, cleanup: bool = False
                ) -> StorageResult[T]:
                    return await _observe(
                        service,
                        ctx,
                        storage,
                        operation,
                        call,
                        lambda result: (result.cost, result.native_operation_id),
                        cleanup=cleanup,
                    )

                verified_capabilities[storage.id] = await probe_private_storage(
                    port, ctx, command.request_id, observe_probe
                )
            if email.status != "READY":
                if email.status != "NOT_CONFIGURED" or email.provider != "demo_email":
                    raise HTTPException(409, "INGRESS_SETUP_CONNECTION_CHANGED")
                adapter = DemoEmailAdapter(service.sessions, ctx.tenant_id, email.id)
                email_capabilities = await adapter.describe_capabilities()
                if not {"get_message", "verify_webhook", "durable_native_ledger", "synthetic"}.issubset(
                    email_capabilities.features
                ):
                    raise HTTPException(503, "INGRESS_SETUP_EMAIL_CAPABILITY")
                observed = await _observe(
                    service,
                    ctx,
                    email,
                    "validate_inbound_source",
                    lambda: adapter.validate_inbound_source(ctx),
                    lambda value: (value.cost, value.provider_ref.external_id),
                )
                if not observed.value.connected or observed.value.mode != "SIMULATED":
                    raise HTTPException(503, "INGRESS_SETUP_EMAIL_UNVERIFIED")
                verified_capabilities[email.id] = sorted(email_capabilities.features)
            await db.run_sync(lambda store: authorize(store, ctx, service))
            current_rows: list[ProviderConnection] = []
            for key in sorted(expected):
                current = await db.get(
                    ProviderConnection, key, with_for_update={"key_share": True}, populate_existing=True
                )
                if (
                    current is None
                    or (current.version, current.status, current.provider, current.external_account_id)
                    != expected[key]
                ):
                    raise HTTPException(409, "INGRESS_SETUP_CONNECTION_CHANGED")
                current_rows.append(current)
            for current in current_rows:
                if current.id in verified_capabilities:
                    current.status, current.capabilities = "READY", verified_capabilities[current.id]
                    current.version += 1
            await db.run_sync(
                lambda store: IdentityService(store).audit(
                    ctx,
                    "email.ingress_setup_ready",
                    command.request_id,
                    {
                        "email_connection_id": str(email_id),
                        "storage_connection_id": str(storage_id),
                        "synthetic": True,
                    },
                )
            )
        except Exception as error:
            failed = error
            reason = str(error.detail) if isinstance(error, HTTPException) else "PROBE_UNVERIFIED"
            # Retain the request/connections and observed usage. No failed probe is promoted to READY.
            await db.run_sync(
                lambda store: IdentityService(store).audit(
                    ctx,
                    "email.ingress_setup_failed",
                    command.request_id,
                    {
                        "reason": reason,
                        "synthetic": True,
                    },
                )
            )
    if failed is not None:
        raise HTTPException(503, "INGRESS_SETUP_PROBE_FAILED") from failed
    async with service.sessions.begin() as db:
        return await db.run_sync(lambda store: setup(store, ctx, service))
