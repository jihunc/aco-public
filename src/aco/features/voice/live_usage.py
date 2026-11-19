"""Per-wire canonical intent/results and saved-profile publication under sealed Voice authority."""

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from aco.core.types import CostQuality, ProviderCost
from aco.features.identity.models import AuditEvent, ProviderConnection, now
from aco.features.usage.dto import ProviderUsageInput, Units
from aco.features.usage.models import ProviderUsageEvent, UsageAttempt, UsageRevision, UsageSource
from aco.features.usage.service import UsageService
from aco.features.voice.live import (
    VoiceLiveAuthorization,
    VoiceLiveGrant,
    VoiceLiveProfile,
    VoiceLiveRequest,
    config_for,
)
from aco.features.voice.models import VoiceSession
from aco.integrations.ports.voice import CallContext, VoiceSetupContext
from aco.integrations.vapi.profile import SavedVoiceProfile


@dataclass(frozen=True)
class _Attribution:
    """Attribution from retained intent only; never passed as business/action authority."""

    tenant_id: UUID
    actor_id: UUID
    correlation_id: UUID


class VoiceLiveUsageObserver:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        authorization: VoiceLiveAuthorization,
        *,
        current: Callable[[Session, CallContext | VoiceSetupContext, str], None],
    ):
        self.sessions, self.authorization, self.current = sessions, authorization, current

    async def started(self, ctx: CallContext | VoiceSetupContext, operation: str) -> UUID:
        async with self.sessions.begin() as db:

            def intent(store: Session) -> UUID:
                self.current(store, ctx, operation)
                row = (
                    store.get(VoiceSession, ctx.session_id, with_for_update=True)
                    if isinstance(ctx, CallContext)
                    else None
                )
                if row is not None and operation == "end_call":
                    retained = store.scalar(
                        select(VoiceLiveRequest).where(
                            VoiceLiveRequest.tenant_id == row.tenant_id,
                            VoiceLiveRequest.grant_id == self.authorization.grant_id,
                            VoiceLiveRequest.session_id == row.id,
                            VoiceLiveRequest.operation == "end_call",
                        )
                    )
                    attempt = (
                        store.get(UsageAttempt, retained.attempt_id, with_for_update=True)
                        if retained
                        else None
                    )
                    if attempt is None or attempt.outcome != "PENDING" or attempt.event_id is not None:
                        raise HTTPException(409, "VOICE_LIVE_STOP_ALREADY_ATTEMPTED")
                    attempt.outcome = "ATTEMPTED"
                    return attempt.id
                if row is not None and operation == "start_browser":
                    attempt = store.get(UsageAttempt, row.usage_attempt_id, populate_existing=True)
                    if attempt is None:
                        raise HTTPException(403, "VOICE_LIVE_START_INTENT_REQUIRED")
                else:
                    attempt = UsageAttempt(
                        tenant_id=ctx.tenant_id,
                        provider_connection_id=ctx.connection_id,
                        actor_id=ctx.actor_id,
                        correlation_id=ctx.correlation_id,
                        operation="voice_" + operation,
                        outcome="PENDING",
                    )
                    store.add(attempt)
                    store.flush()
                self.authorization.reserve(
                    store,
                    ctx,
                    operation,
                    attempt_id=attempt.id,
                    session_id=ctx.session_id if isinstance(ctx, CallContext) else None,
                )
                if row is not None and operation == "start_browser":
                    # Reserve exact-call safety-stop headroom atomically before creating a call.
                    # PENDING is not a wire observation; actual stop claims this intent once.
                    stop = UsageAttempt(
                        tenant_id=row.tenant_id,
                        provider_connection_id=row.connection_id,
                        actor_id=row.actor_id,
                        correlation_id=row.correlation_id,
                        operation="voice_end_call",
                        outcome="PENDING",
                    )
                    store.add(stop)
                    store.flush()
                    self.authorization.reserve(store, ctx, "end_call", attempt_id=stop.id, session_id=row.id)
                return attempt.id

            return await db.run_sync(intent)

    async def finished(self, key: UUID, operation: str, *, native: str | None, failure: str | None) -> None:
        async with self.sessions.begin() as db:

            def result(store: Session) -> None:
                attempt = store.get(UsageAttempt, key, with_for_update=True, populate_existing=True)
                link = store.scalar(select(VoiceLiveRequest).where(VoiceLiveRequest.attempt_id == key))
                if (
                    attempt is None
                    or link is None
                    or link.grant_id != self.authorization.grant_id
                    or link.operation != operation
                    or attempt.operation != "voice_" + operation
                ):
                    raise HTTPException(403, "VOICE_LIVE_RETAINED_INTENT_REQUIRED")
                outcome = (
                    "NO_EFFECT"
                    if failure == "NO_EFFECT"
                    else "SUCCEEDED"
                    if failure is None
                    else "UNKNOWN"
                    if failure == "UNKNOWN_SIDE_EFFECT"
                    else "FAILED"
                )
                if attempt.event_id is not None:
                    if attempt.outcome != outcome:
                        raise HTTPException(409, "VOICE_LIVE_OBSERVATION_CONFLICT")
                    return
                attempt.outcome, attempt.error_reference = outcome, failure
                attempt.business_outcome = "OBSERVED" if not failure else "FAILED"
                if outcome == "NO_EFFECT":
                    # Permanent request fence and conservative exposure remain; no fictitious wire usage.
                    return
                grant = store.get(VoiceLiveGrant, link.grant_id)
                if grant is None or grant.tenant_id != attempt.tenant_id:
                    raise HTTPException(403, "VOICE_LIVE_RETAINED_INTENT_REQUIRED")
                # Retain completion attribution after kill/revocation; this grants no new wire/action.
                config = config_for(grant)
                prefix = (
                    "assistant"
                    if operation == "probe_assistant"
                    else "credential"
                    if operation == "probe_credential"
                    else "call"
                )
                subject = str(UUID(native)) if native is not None else None
                if operation.startswith("probe_") and subject != str(
                    config.assistant_id if operation == "probe_assistant" else config.server_credential_id
                ):
                    if failure is None:
                        raise HTTPException(403, "VOICE_LIVE_OBSERVATION_SCOPE_DENIED")
                attribution = _Attribution(attempt.tenant_id, attempt.actor_id, attempt.correlation_id)
                event = UsageService(store).record_verified(
                    attribution,
                    ProviderUsageInput(
                        provider_connection_id=attempt.provider_connection_id,
                        operation_key=str(attempt.id),
                        component="voice_live_" + operation,
                        operation=operation,
                        provider_kind="voice",
                        provider="vapi",
                        synthetic=False,
                        cost=ProviderCost(amount=None, currency="USD", quality=CostQuality.UNKNOWN),
                        units=Units(requests=1),
                        native_operation_id=None,
                        native_subject=f"{prefix}:{subject}" if subject else None,
                        outcome=outcome,
                    ),
                )
                attempt.event_id = event.id
                if link.session_id is not None:
                    store.add(
                        UsageSource(
                            event_id=event.id,
                            source_kind="voice_session",
                            source_id=link.session_id,
                            component=f"voice_live_{operation}:{attempt.id}",
                        )
                    )

            await db.run_sync(result)


def publish_profile(
    db: Session, authorization: VoiceLiveAuthorization, ctx: VoiceSetupContext, proof: SavedVoiceProfile
) -> VoiceLiveProfile:
    grant = authorization.validate(db, ctx.tenant_id, ctx.connection_id, require_ready=False)
    config = config_for(grant)
    if (
        ctx.actor_id != grant.initiator_id
        or ctx.connection_version != grant.connection_version
        or ctx.tenant_version != grant.tenant_generation
        or ctx.membership_version != grant.membership_version
        or ctx.actor_version != grant.actor_version
        or ctx.binding_hash != config.binding_sha256
        or ctx.expires_at <= now()
        or proof.assistant_profile_sha256 != config.assistant_profile_sha256
    ):
        raise HTTPException(403, "VOICE_LIVE_PROFILE_CAPTURE_CHANGED")
    events = []
    for key, operation, subject in (
        (proof.assistant_attempt_id, "probe_assistant", f"assistant:{config.assistant_id}"),
        (proof.credential_attempt_id, "probe_credential", f"credential:{config.server_credential_id}"),
    ):
        attempt = db.get(UsageAttempt, key, populate_existing=True)
        link = db.scalar(select(VoiceLiveRequest).where(VoiceLiveRequest.attempt_id == key))
        event = db.get(ProviderUsageEvent, attempt.event_id) if attempt and attempt.event_id else None
        revision = (
            db.scalar(select(UsageRevision).where(UsageRevision.event_id == event.id)) if event else None
        )
        if (
            attempt is None
            or link is None
            or link.grant_id != grant.id
            or link.session_id is not None
            or link.operation != operation
            or attempt.actor_id != ctx.actor_id
            or attempt.correlation_id != ctx.correlation_id
            or attempt.outcome != "SUCCEEDED"
            or event is None
            or event.tenant_id != ctx.tenant_id
            or event.provider_connection_id != ctx.connection_id
            or event.provider != "vapi"
            or event.provider_kind != "voice"
            or event.synthetic
            or event.native_subject != subject
            or event.operation != operation
            or revision is None
            or revision.outcome != "SUCCEEDED"
        ):
            raise HTTPException(403, "VOICE_LIVE_PROFILE_OBSERVATION_REQUIRED")
        events.append(event.id)
    old = db.scalar(select(VoiceLiveProfile).where(VoiceLiveProfile.grant_id == grant.id))
    if old is not None:
        if (old.assistant_usage_event_id, old.credential_usage_event_id) != tuple(events):
            raise HTTPException(409, "VOICE_LIVE_PROFILE_REPLAY_CONFLICT")
        return old
    connection = db.get(ProviderConnection, ctx.connection_id, with_for_update=True, populate_existing=True)
    if (
        connection is None
        or connection.status != "NOT_CONFIGURED"
        or connection.version != ctx.connection_version
    ):
        raise HTTPException(403, "VOICE_LIVE_PROFILE_CONNECTION_CHANGED")
    connection.version += 1
    connection.status = "READY"
    connection.capabilities = ["browser_voice", "LIVE", "capture_off", "probe:" + config.binding_sha256]
    row = VoiceLiveProfile(
        tenant_id=ctx.tenant_id,
        grant_id=grant.id,
        connection_version=connection.version,
        binding_sha256=config.binding_sha256,
        assistant_profile_sha256=config.assistant_profile_sha256,
        assistant_usage_event_id=events[0],
        credential_usage_event_id=events[1],
    )
    db.add(row)
    db.add(
        AuditEvent(
            tenant_id=ctx.tenant_id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.correlation_id,
            action="voice.saved_live_profile_verified",
            target_id=connection.id,
            details={
                "grant_id": str(grant.id),
                "capture": "DISABLED",
                "profile_sha256": config.assistant_profile_sha256,
            },
        )
    )
    db.flush()
    authorization.validate(db, ctx.tenant_id, ctx.connection_id, require_ready=False)
    return row
