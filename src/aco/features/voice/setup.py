"""Explicit local synthetic connection probe; never credential-driven call activation."""

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.features.identity.models import Actor, Membership, ProviderConnection, now
from aco.features.identity.service import Permission
from aco.features.usage.models import UsageAttempt
from aco.features.voice.dto import SetupVoice, VoicePage
from aco.features.voice.service import VoiceService
from aco.features.voice.usage import record_voice_usage
from aco.integrations.ports.voice import VoiceFailure, VoiceSetupContext

if TYPE_CHECKING:
    from aco.features.voice.runtime import VoiceRuntime


def connection_key(tenant_id: UUID) -> UUID:
    return uuid5(tenant_id, "aco.synthetic.voice.connection.v1")


def setup_authorized(db: Session, runtime: "VoiceRuntime", ctx: TenantContext) -> tuple[int, int, int]:
    tenant = VoiceService(db, runtime).human(ctx, Permission.SETTINGS)
    if not runtime.setup_enabled or runtime.environment != "local" or not tenant.is_demo:
        raise HTTPException(403, "VOICE_SETUP_DISABLED")
    binding = runtime.binding(ctx.tenant_id, connection_key(ctx.tenant_id))
    if binding.server_credential_id is None:
        raise HTTPException(403, "VOICE_SETUP_PROFILE_REQUIRED")
    member = db.scalar(
        select(Membership)
        .where(Membership.tenant_id == ctx.tenant_id, Membership.user_id == ctx.actor_id)
        .execution_options(populate_existing=True)
    )
    actor = db.get(Actor, ctx.actor_id, populate_existing=True)
    if member is None or actor is None or not actor.active or actor.kind != "human":
        raise HTTPException(403, "VOICE_SETUP_HUMAN_REQUIRED")
    return tenant.version, member.version, actor.version


async def prepare_voice_connection(
    runtime: "VoiceRuntime", ctx: TenantContext, command: SetupVoice
) -> VoicePage:
    async with runtime.sessions.begin() as db:

        def stage(store: Session) -> tuple[VoiceSetupContext, UUID] | VoicePage:
            tenant_version, membership_version, actor_version = setup_authorized(store, runtime, ctx)
            key = connection_key(ctx.tenant_id)
            binding = runtime.binding(ctx.tenant_id, key)
            fingerprint = runtime.binding_hash(binding)
            row = store.get(ProviderConnection, key, with_for_update=True)
            if row is not None and (
                row.tenant_id != ctx.tenant_id or row.provider != "vapi" or row.provider_type != "voice"
            ):
                raise HTTPException(403, "VOICE_SETUP_CONNECTION_CONFLICT")
            if (
                row is not None
                and row.status == "READY"
                and row.encrypted_credentials_ref == binding.credential_revision
                and "probe:" + fingerprint in row.capabilities
            ):
                return VoiceService(store, runtime).list(ctx)
            if row is None:
                row = ProviderConnection(
                    id=key, tenant_id=ctx.tenant_id, provider="vapi", provider_type="voice"
                )
                store.add(row)
            else:
                row.version += 1
            row.status = "NOT_CONFIGURED"
            row.encrypted_credentials_ref = binding.credential_revision
            row.capabilities = []
            row.external_account_id = "synthetic-assistant:" + str(binding.assistant_id)
            attempt = UsageAttempt(
                tenant_id=ctx.tenant_id,
                provider_connection_id=key,
                actor_id=ctx.actor_id,
                correlation_id=ctx.correlation_id,
                operation="probe_assistant",
                outcome="PENDING",
            )
            store.flush()
            store.add(attempt)
            store.flush()
            return VoiceSetupContext(
                tenant_id=ctx.tenant_id,
                connection_id=key,
                actor_id=ctx.actor_id,
                correlation_id=ctx.correlation_id,
                expires_at=now() + timedelta(minutes=1),
                connection_version=row.version,
                tenant_version=tenant_version,
                membership_version=membership_version,
                actor_version=actor_version,
                binding_hash=fingerprint,
            ), attempt.id

        staged = await db.run_sync(stage)
    if isinstance(staged, VoicePage):
        return staged
    probe, attempt_id = staged
    attempted = False

    def current(store: Session, received: VoiceSetupContext) -> ProviderConnection:
        if (
            received != probe
            or received.actor_id != ctx.actor_id
            or received.tenant_id != ctx.tenant_id
            or runtime.clock.now() >= received.expires_at
        ):
            raise HTTPException(403, "VOICE_SETUP_SCOPE_CHANGED")
        if setup_authorized(store, runtime, ctx) != (
            probe.tenant_version,
            probe.membership_version,
            probe.actor_version,
        ):
            raise HTTPException(403, "VOICE_SETUP_AUTHORITY_CHANGED")
        row = store.get(ProviderConnection, probe.connection_id, with_for_update=True, populate_existing=True)
        if (
            row is None
            or row.tenant_id != ctx.tenant_id
            or row.provider != "vapi"
            or row.provider_type != "voice"
            or row.encrypted_credentials_ref != runtime.binding(ctx.tenant_id, row.id).credential_revision
            or row.status != "NOT_CONFIGURED"
            or row.version != probe.connection_version
            or runtime.binding_hash(runtime.binding(ctx.tenant_id, row.id)) != probe.binding_hash
        ):
            raise HTTPException(403, "VOICE_SETUP_CONFIGURATION_CHANGED")
        return row

    async def authorize(received: VoiceSetupContext) -> None:
        nonlocal attempted
        async with runtime.sessions.begin() as db:
            await db.run_sync(lambda store: current(store, received))
        attempted = True

    failure: VoiceFailure | HTTPException | None = None
    try:
        await runtime.port(ctx.tenant_id, probe.connection_id).probe_assistant(probe, authorize)
    except (VoiceFailure, HTTPException) as exc:
        failure = exc
    async with runtime.sessions.begin() as db:

        def usage(store: Session) -> None:
            attempt = store.get(UsageAttempt, attempt_id)
            assert attempt
            attempt.outcome = "FAILED" if failure else "SUCCESS"
            attempt.business_outcome = "FAILED" if failure else "PENDING"
            if not attempted:
                attempt.outcome = "NO_EFFECT"
            if attempted:
                record_voice_usage(
                    store,
                    ctx,
                    attempt,
                    component="voice_setup",
                    operation="probe_assistant",
                    outcome=attempt.outcome,
                )

        await db.run_sync(usage)
    if failure:
        if isinstance(failure, HTTPException):
            raise failure
        raise HTTPException(503, "VOICE_SETUP_PROBE_FAILED") from failure
    try:
        async with runtime.sessions.begin() as db:

            def publish(store: Session) -> VoicePage:
                row = current(store, probe)
                row.status = "READY"
                row.capabilities = [
                    "browser_voice",
                    "MOCKED_HTTP",
                    "capture_off",
                    "probe:" + probe.binding_hash,
                ]
                row.version += 1
                from aco.features.identity.service import IdentityService

                IdentityService(store).audit(
                    ctx,
                    "voice.synthetic_setup_verified",
                    row.id,
                    {
                        "evidence_mode": "MOCKED_HTTP",
                        "request_id": str(command.request_id),
                        "capture": "DISABLED",
                    },
                )
                attempt = store.get(UsageAttempt, attempt_id)
                assert attempt
                attempt.business_outcome = "COMMITTED"
                return VoiceService(store, runtime).list(ctx)

            return await db.run_sync(publish)
    except HTTPException:
        async with runtime.sessions.begin() as db:
            attempt = await db.get(UsageAttempt, attempt_id)
            assert attempt
            attempt.business_outcome = "DENIED"
        raise
