"""Explicit LIVE-code Voice composition; private join capabilities never enter history.

New work, a bounded exact-call safety stop, and signed terminal attribution have
separate authority paths. This module never installs a grant or selects credentials.
"""

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from aco.core.clock import Clock
from aco.core.context import TenantContext
from aco.features.identity.models import ProviderConnection, now
from aco.features.identity.service import IdentityService, Permission
from aco.features.policies.guards import PolicyGuards
from aco.features.usage.models import ProviderUsageEvent, UsageAttempt, UsageRevision
from aco.features.voice.dto import (
    PrepareVoice,
    SetupVoice,
    VoiceJoinView,
    VoicePage,
    VoiceSessionView,
    VoiceStopOutcome,
)
from aco.features.voice.live import (
    VoiceBrowserJoin,
    VoiceLiveAuthorization,
    VoiceLiveConfig,
    VoiceLiveGrant,
    VoiceLiveProfile,
    VoiceLiveRequest,
    config_for,
)
from aco.features.voice.live_policy import voice_policy_snapshot
from aco.features.voice.live_usage import VoiceLiveUsageObserver, publish_profile
from aco.features.voice.models import VoiceSession
from aco.features.voice.runtime import VoiceRuntime
from aco.features.voice.service import VoiceService
from aco.integrations.ports.voice import (
    CallContext,
    ProviderCallRef,
    VerifiedVoiceWebhook,
    VoiceFailure,
    VoiceSetupContext,
)
from aco.integrations.vapi.adapter import VapiVoiceAdapter
from aco.integrations.vapi.profile import VapiSavedProfileProbe
from aco.integrations.vapi.settings import VapiBinding


class LiveVoiceRuntime(VoiceRuntime):
    evidence_mode = "LIVE"

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        binding: VapiBinding,
        *,
        authorization: VoiceLiveAuthorization,
        capability_secret: SecretStr,
        join_encryption_key: SecretStr,
        transport_factory: Callable[[], httpx.AsyncBaseTransport],
        clock: Clock | None = None,
    ):
        if binding.evidence_mode != "LIVE" or binding.control_api_key is None or not binding.control_hosts:
            raise ValueError("VOICE_LIVE_BINDING_REQUIRED")
        super().__init__(
            sessions,
            lambda tenant, connection: binding,
            capability_secret=capability_secret,
            transport_factory=transport_factory,
            enabled=True,
            clock=clock,
        )
        self.bound, self.authorization = binding, authorization
        self.cipher = Fernet(join_encryption_key.get_secret_value().encode())
        self.observer = VoiceLiveUsageObserver(sessions, authorization, current=self.wire_current)

    def binding(self, tenant_id: UUID, connection_id: UUID) -> VapiBinding:
        if (tenant_id, connection_id) != (self.bound.tenant_id, self.bound.connection_id):
            raise HTTPException(403, "VOICE_LIVE_BINDING_UNAVAILABLE")
        if not self.authorization.integrity_check():
            raise HTTPException(403, "VOICE_LIVE_CONFIGURATION_CHANGED")
        return self.bound

    def grant(
        self,
        db: Session,
        tenant_id: UUID,
        connection_id: UUID,
        *,
        require_ready: bool = True,
        safety_stop: bool = False,
    ) -> VoiceLiveGrant:
        grant = self.authorization.validate(
            db, tenant_id, connection_id, require_ready=require_ready, safety_stop=safety_stop
        )
        if self.binding_hash(self.binding(tenant_id, connection_id)) != config_for(grant).binding_sha256:
            raise HTTPException(403, "VOICE_LIVE_CONFIGURATION_CHANGED")
        return grant

    def setup_authorized(self, db: Session, ctx: TenantContext) -> None:
        VoiceService(db, self).human(ctx, Permission.SETTINGS)
        grant = self.grant(db, ctx.tenant_id, self.bound.connection_id, require_ready=False)
        if (
            ctx.actor_id != grant.initiator_id
            or self.binding_hash(self.bound) != config_for(grant).binding_sha256
        ):
            raise HTTPException(403, "VOICE_LIVE_SETUP_SCOPE_DENIED")

    async def setup(self, ctx: TenantContext, command: SetupVoice) -> VoicePage:
        async with self.sessions.begin() as db:

            def prepare(store: Session) -> VoiceSetupContext | VoicePage:
                self.setup_authorized(store, ctx)
                grant = store.get(VoiceLiveGrant, self.authorization.grant_id)
                assert grant is not None
                if store.scalar(select(VoiceLiveProfile.id).where(VoiceLiveProfile.grant_id == grant.id)):
                    return VoiceService(store, self).list(ctx)
                config = config_for(grant)
                return VoiceSetupContext(
                    tenant_id=ctx.tenant_id,
                    connection_id=grant.connection_id,
                    actor_id=ctx.actor_id,
                    correlation_id=ctx.correlation_id,
                    expires_at=min(now() + timedelta(minutes=1), config.expires_at),
                    connection_version=grant.connection_version,
                    tenant_version=grant.tenant_generation,
                    membership_version=grant.membership_version,
                    actor_version=grant.actor_version,
                    binding_hash=self.binding_hash(self.bound),
                )

            staged = await db.run_sync(prepare)
        if isinstance(staged, VoicePage):
            return staged
        async with self.sessions.begin() as db:

            def configured(store: Session) -> VoiceLiveConfig:
                grant = store.get(VoiceLiveGrant, self.authorization.grant_id)
                assert grant is not None
                return config_for(grant)

            config = await db.run_sync(configured)
        assert self.transport_factory is not None

        async def authorize(received: VoiceSetupContext) -> None:
            async with self.sessions.begin() as db:
                await db.run_sync(lambda store: self.wire_current(store, received, "probe_assistant"))

        try:
            proof = await VapiSavedProfileProbe(
                self.bound, observer=self.observer, transport=self.transport_factory()
            ).verify(
                staged,
                authorize,
                account_id=config.account_id,
                callback_url=config.callback_url,
                expected_sha256=config.assistant_profile_sha256,
            )
        except VoiceFailure as exc:
            raise HTTPException(403, "VOICE_LIVE_PROFILE_UNVERIFIED") from exc
        async with self.sessions.begin() as db:

            def publish(store: Session) -> VoicePage:
                publish_profile(store, self.authorization, staged, proof)
                return VoiceService(store, self).list(ctx)

            return await db.run_sync(publish)

    def connection_available(self, db: Session, ctx: TenantContext, connection_id: UUID) -> VapiBinding:
        binding = self.binding(ctx.tenant_id, connection_id)
        grant = self.grant(db, ctx.tenant_id, connection_id)
        if ctx.actor_id != grant.initiator_id:
            raise HTTPException(403, "VOICE_LIVE_INITIATOR_REQUIRED")
        return binding

    def stop_outcome(self, db: Session, row: VoiceSession) -> VoiceStopOutcome:
        link = db.scalar(
            select(VoiceLiveRequest).where(
                VoiceLiveRequest.tenant_id == row.tenant_id,
                VoiceLiveRequest.session_id == row.id,
                VoiceLiveRequest.grant_id == row.live_grant_id,
                VoiceLiveRequest.operation == "end_call",
            )
        )
        attempt = db.get(UsageAttempt, link.attempt_id, populate_existing=True) if link else None
        states: dict[str, VoiceStopOutcome] = {
            "PENDING": "RESERVED",
            "ATTEMPTED": "ATTEMPTED",
            "SUCCEEDED": "ACCEPTED",
            "UNKNOWN": "UNKNOWN",
        }
        return states.get(attempt.outcome, "FAILED") if attempt else "NOT_REQUESTED"

    def preparation_binding(
        self, db: Session, command: PrepareVoice, initiator_id: UUID
    ) -> tuple[UUID, datetime]:
        grant = self.grant(db, self.bound.tenant_id, command.connection_id)
        if (
            grant.initiator_id != initiator_id
            or command.consent_id is not None
            or command.demo_scenario != "task"
        ):
            raise HTTPException(403, "VOICE_LIVE_PREPARATION_SCOPE_DENIED")
        _, reasons = voice_policy_snapshot(db, grant, command.invoice_ids, self.clock.now())
        if reasons:
            raise HTTPException(403, reasons[0])
        return grant.id, min(now() + timedelta(minutes=5), config_for(grant).expires_at)

    def approval_metadata(self, db: Session, row: VoiceSession) -> dict[str, object]:
        grant = self.grant(db, row.tenant_id, row.connection_id)
        config = config_for(grant)
        return dict(
            evidence_mode="LIVE",
            participant=config.participant_mode,
            live_grant_id=grant.id,
            participant_id=grant.participant_id,
            participant_email=config.participant_email,
            max_duration_seconds=config.max_call_seconds,
        )

    def start_available(self, db: Session, ctx: TenantContext, row: VoiceSession) -> None:
        if ctx.actor_id != row.initiator_id:
            raise HTTPException(403, "VOICE_LIVE_INITIATOR_REQUIRED")
        grant = self.grant(db, row.tenant_id, row.connection_id)
        command = PrepareVoice.model_validate_json(json.dumps(row.command))
        _, reasons = voice_policy_snapshot(
            db, grant, command.invoice_ids, self.clock.now(), exclude_operation_id=row.request_id
        )
        if reasons:
            raise HTTPException(403, reasons[0])

    def before_start(self, db: Session, ctx: TenantContext, row: VoiceSession) -> None:
        self.start_available(db, ctx, row)
        grant = self.grant(db, row.tenant_id, row.connection_id)
        command = PrepareVoice.model_validate_json(json.dumps(row.command))
        # The genuine current initiating human is already resolved; no service-context promotion.
        from aco.features.accounting.models import InvoiceSnapshot

        invoice = db.get(InvoiceSnapshot, command.invoice_ids[0])
        assert invoice is not None
        reservation = PolicyGuards(db, at=self.clock.now()).reserve_contact(
            ctx,
            invoice.id,
            config_for(grant).participant_email,
            "voice",
            row.request_id,
        )
        reservation.status = "inflight"

    def control_permissions(self, db: Session, ctx: TenantContext, row: VoiceSession) -> dict[str, object]:
        grant = db.get(VoiceLiveGrant, row.live_grant_id) if row.live_grant_id else None
        if grant is None:
            return dict(
                can_join=False,
                join_reasons=["VOICE_LIVE_GRANT_REQUIRED"],
                can_end=False,
                end_reasons=["VOICE_LIVE_GRANT_REQUIRED"],
            )
        join_reasons, end_reasons = [], []
        try:
            VoiceService(db, self).human(ctx, Permission.INVESTIGATE)
            self.current(db, self.context(row))
            if ctx.actor_id != grant.participant_id or row.state not in {"active", "connected"}:
                raise HTTPException(403, "VOICE_LIVE_PARTICIPANT_REQUIRED")
            if self.stop_attempted(db, row):
                raise HTTPException(409, "VOICE_LIVE_STOP_REQUESTED")
            command = PrepareVoice.model_validate_json(json.dumps(row.command))
            _, reasons = voice_policy_snapshot(
                db, grant, command.invoice_ids, self.clock.now(), exclude_operation_id=row.request_id
            )
            if reasons:
                raise HTTPException(403, reasons[0])
            retained, material = self.private_material(db, row)
            if retained.expires_at <= now() or not isinstance(material.get("join"), str):
                raise HTTPException(409, "VOICE_LIVE_JOIN_UNAVAILABLE")
        except HTTPException as exc:
            join_reasons.append(str(exc.detail))
        try:
            VoiceService(db, self).human(ctx, Permission.INVESTIGATE)
            if ctx.actor_id not in {row.initiator_id, grant.participant_id}:
                raise HTTPException(403, "VOICE_LIVE_STOP_SCOPE_DENIED")
            self.stop_current(db, self.context(row))
            _, material = self.private_material(db, row)
            if not isinstance(material.get("control"), str):
                raise HTTPException(409, "VOICE_LIVE_STOP_UNAVAILABLE")
            if self.stop_attempted(db, row):
                raise HTTPException(409, "VOICE_LIVE_STOP_ALREADY_ATTEMPTED")
        except HTTPException as exc:
            end_reasons.append(str(exc.detail))
        return dict(
            participant_id=grant.participant_id,
            participant_email=config_for(grant).participant_email,
            can_join=not join_reasons,
            join_reasons=join_reasons,
            can_end=not end_reasons,
            end_reasons=end_reasons,
        )

    def start_operation(self) -> str:
        return "voice_start_browser"

    def wire_current(self, db: Session, ctx: CallContext | VoiceSetupContext, operation: str) -> None:
        if isinstance(ctx, VoiceSetupContext):
            grant = self.grant(db, ctx.tenant_id, ctx.connection_id, require_ready=False)
            if (
                operation not in {"probe_assistant", "probe_credential"}
                or ctx.actor_id != grant.initiator_id
                or ctx.binding_hash != self.binding_hash(self.bound)
                or ctx.expires_at <= now()
                or (ctx.connection_version, ctx.tenant_version, ctx.membership_version, ctx.actor_version)
                != (
                    grant.connection_version,
                    grant.tenant_generation,
                    grant.membership_version,
                    grant.actor_version,
                )
            ):
                raise HTTPException(403, "VOICE_LIVE_SETUP_SCOPE_DENIED")
            return
        if operation == "end_call":
            self.stop_current(db, ctx)
        else:
            self.grant(db, ctx.tenant_id, ctx.connection_id)
            self.current(db, ctx, allow_start=operation == "start_browser")

    async def authorize(self, ctx: CallContext, operation: str, material: object) -> None:
        if (
            operation == "verify_webhook"
            and isinstance(material, VerifiedVoiceWebhook)
            and self.terminal_only(material)
        ):
            async with self.sessions.begin() as db:
                await db.run_sync(lambda store: self.terminal_current(store, ctx))
            return
        if operation == "end_call":
            async with self.sessions.begin() as db:
                await db.run_sync(lambda store: self.stop_current(store, ctx))
            return
        self.authorization.validate_integrity()
        if self.authorization.kill_switch():
            raise HTTPException(403, "VOICE_LIVE_KILL_SWITCH")
        await super().authorize(ctx, operation, material)

    def current(
        self,
        db: Session,
        ctx: CallContext,
        *,
        allow_start: bool = False,
        allow_own_stop_completion: bool = False,
    ) -> VoiceSession:
        self.grant(db, ctx.tenant_id, ctx.connection_id)
        return super().current(
            db, ctx, allow_start=allow_start, allow_own_stop_completion=allow_own_stop_completion
        )

    def port(self, tenant: UUID, connection: UUID) -> VapiVoiceAdapter:
        assert self.transport_factory is not None
        return VapiVoiceAdapter(
            self.binding(tenant, connection),
            authority=self.authorize,
            enabled=True,
            transport=self.transport_factory(),
            clock=self.clock,
            observer=self.observer,
        )

    async def retain_call(self, key: UUID, result: ProviderCallRef) -> None:
        # Preserve actual native acceptance even when join/control metadata is unavailable.
        material = {
            "join": result.browser_join.get_secret_value() if result.browser_join else None,
            "control": result.control_url.get_secret_value() if result.control_url else None,
        }
        async with self.sessions.begin() as db:

            def retain(store: Session) -> None:
                row = store.get(VoiceSession, key, with_for_update=True)
                assert row is not None and row.live_grant_id == self.authorization.grant_id
                if store.scalar(select(VoiceBrowserJoin.id).where(VoiceBrowserJoin.session_id == row.id)):
                    raise HTTPException(409, "VOICE_LIVE_CAPABILITY_ALREADY_RETAINED")
                store.add(
                    VoiceBrowserJoin(
                        tenant_id=row.tenant_id,
                        session_id=row.id,
                        grant_id=row.live_grant_id,
                        provider_call_id=result.reference.external_id,
                        encrypted_material=self.cipher.encrypt(json.dumps(material).encode()),
                        expires_at=min(
                            row.expires_at, now() + timedelta(seconds=self.bound.max_duration_seconds)
                        ),
                    )
                )

            await db.run_sync(retain)

    def private_material(self, db: Session, row: VoiceSession) -> tuple[VoiceBrowserJoin, dict[str, object]]:
        retained = db.scalar(
            select(VoiceBrowserJoin).where(
                VoiceBrowserJoin.tenant_id == row.tenant_id, VoiceBrowserJoin.session_id == row.id
            )
        )
        if (
            retained is None
            or retained.grant_id != row.live_grant_id
            or retained.provider_call_id != row.provider_call_id
        ):
            raise HTTPException(409, "VOICE_LIVE_CAPABILITY_UNAVAILABLE")
        try:
            value = json.loads(self.cipher.decrypt(retained.encrypted_material))
            if not isinstance(value, dict):
                raise ValueError("Invalid retained capability")
            return retained, value
        except (InvalidToken, ValueError) as exc:
            raise HTTPException(403, "VOICE_LIVE_CAPABILITY_UNAVAILABLE") from exc

    async def join(self, ctx: TenantContext, key: UUID, expected_version: int) -> VoiceJoinView:
        async with self.sessions.begin() as db:

            def read(store: Session) -> VoiceJoinView:
                IdentityService(store).authorize(ctx, Permission.INVESTIGATE)
                row = store.get(VoiceSession, key, with_for_update=True)
                if row is None or row.tenant_id != ctx.tenant_id:
                    raise HTTPException(404, "VOICE_SESSION_NOT_FOUND")
                grant = self.grant(store, row.tenant_id, row.connection_id)
                if (
                    ctx.actor_id != grant.participant_id
                    or row.version != expected_version
                    or row.state not in {"active", "connected"}
                ):
                    raise HTTPException(403, "VOICE_LIVE_PARTICIPANT_REQUIRED")
                self.current(store, self.context(row))
                if self.stop_attempted(store, row):
                    raise HTTPException(409, "VOICE_LIVE_STOP_REQUESTED")
                command = PrepareVoice.model_validate_json(json.dumps(row.command))
                _, reasons = voice_policy_snapshot(
                    store, grant, command.invoice_ids, self.clock.now(), exclude_operation_id=row.request_id
                )
                if reasons:
                    raise HTTPException(403, reasons[0])
                retained, value = self.private_material(store, row)
                if retained.expires_at <= now() or not isinstance(value.get("join"), str):
                    raise HTTPException(409, "VOICE_LIVE_JOIN_UNAVAILABLE")
                assert row.provider_call_id is not None
                join_url = value["join"]
                assert isinstance(join_url, str)
                return VoiceJoinView(
                    session_id=row.id,
                    provider_call_id=row.provider_call_id,
                    web_call_url=join_url,
                    expires_at=retained.expires_at,
                )

            return await db.run_sync(read)

    def terminal_current(self, db: Session, ctx: CallContext) -> VoiceSession:
        self.authorization.validate_integrity()
        row = db.get(VoiceSession, ctx.session_id, with_for_update=True, populate_existing=True)
        grant = db.get(VoiceLiveGrant, self.authorization.grant_id, populate_existing=True)
        connection = db.get(ProviderConnection, ctx.connection_id, populate_existing=True)
        profile = db.scalar(
            select(VoiceLiveProfile).where(VoiceLiveProfile.grant_id == self.authorization.grant_id)
        )
        if (
            row is None
            or grant is None
            or profile is None
            or connection is None
            or (
                row.tenant_id,
                row.connection_id,
                row.actor_id,
                row.correlation_id,
                row.provider_call_id,
                row.expires_at,
            )
            != (
                ctx.tenant_id,
                ctx.connection_id,
                ctx.actor_id,
                ctx.correlation_id,
                ctx.provider_call_id,
                ctx.expires_at,
            )
            or not row.provider_call_id
            or row.live_grant_id != grant.id
            or grant.tenant_id != row.tenant_id
            or grant.connection_id != row.connection_id
            or connection.tenant_id != row.tenant_id
            or connection.version != row.connection_version
            or connection.version != profile.connection_version
            or connection.status != "READY"
            or connection.provider != "vapi"
            or connection.provider_type != "voice"
            or connection.external_account_id != config_for(grant).account_id
            or connection.encrypted_credentials_ref != row.credential_revision
            or self.binding_hash(self.binding(row.tenant_id, row.connection_id)) != row.binding_hash
            or (config_for(grant).authorization_ref, config_for(grant).source_commit)
            != (self.authorization.authorization_ref, self.authorization.source_commit)
            or self.binding_hash(self.bound) != config_for(grant).binding_sha256
            or row.state not in {"active", "connected", "completed", "unknown_outcome"}
        ):
            raise HTTPException(403, "VOICE_LIVE_RETAINED_CALL_REQUIRED")
        attempt = db.get(UsageAttempt, row.usage_attempt_id) if row.usage_attempt_id else None
        link = db.scalar(
            select(VoiceLiveRequest).where(
                VoiceLiveRequest.session_id == row.id,
                VoiceLiveRequest.grant_id == grant.id,
                VoiceLiveRequest.operation == "start_browser",
            )
        )
        observed = db.get(ProviderUsageEvent, attempt.event_id) if attempt and attempt.event_id else None
        revision = (
            db.scalar(select(UsageRevision).where(UsageRevision.event_id == observed.id))
            if observed
            else None
        )
        if (
            attempt is None
            or link is None
            or link.attempt_id != attempt.id
            or (attempt.tenant_id, attempt.provider_connection_id, attempt.actor_id, attempt.correlation_id)
            != (row.tenant_id, row.connection_id, row.actor_id, row.correlation_id)
            or attempt.operation != "voice_start_browser"
            or attempt.outcome != "SUCCEEDED"
            or observed is None
            or observed.tenant_id != row.tenant_id
            or observed.provider_connection_id != row.connection_id
            or observed.provider != "vapi"
            or observed.provider_kind != "voice"
            or observed.synthetic
            or observed.operation != "start_browser"
            or observed.native_subject != f"call:{row.provider_call_id}"
            or revision is None
            or revision.outcome != "SUCCEEDED"
        ):
            raise HTTPException(403, "VOICE_LIVE_ACCEPTED_NATIVE_REQUIRED")
        return row

    def stop_current(self, db: Session, ctx: CallContext) -> VoiceSession:
        # Mutable kill alone cannot prevent a separately budgeted stop of this exact call.
        self.grant(db, ctx.tenant_id, ctx.connection_id, safety_stop=True)
        row = self.terminal_current(db, ctx)
        if row.state == "completed":
            raise HTTPException(409, "VOICE_CALL_ENDED")
        return row

    @staticmethod
    def terminal_only(verified: VerifiedVoiceWebhook) -> bool:
        return bool(verified.events) and all(event.kind == "call.completed" for event in verified.events)

    def callback_expiry_allowed(self, row: VoiceSession) -> bool:
        return row.live_grant_id == self.authorization.grant_id and row.provider_call_id is not None

    def callback_current(self, db: Session, ctx: CallContext, verified: VerifiedVoiceWebhook) -> VoiceSession:
        if self.terminal_only(verified):
            return self.terminal_current(db, ctx)
        row = self.current(db, ctx, allow_own_stop_completion=True)
        if self.stop_attempted(db, row):
            raise HTTPException(409, "VOICE_LIVE_STOP_REQUESTED")
        return row

    @staticmethod
    def stop_attempted(db: Session, row: VoiceSession) -> bool:
        link = db.scalar(
            select(VoiceLiveRequest).where(
                VoiceLiveRequest.tenant_id == row.tenant_id,
                VoiceLiveRequest.session_id == row.id,
                VoiceLiveRequest.operation == "end_call",
            )
        )
        attempt = db.get(UsageAttempt, link.attempt_id) if link else None
        return attempt is not None and attempt.outcome != "PENDING"

    async def end(self, ctx: TenantContext, key: UUID, expected_version: int) -> VoiceSessionView:
        async with self.sessions.begin() as db:

            def stage(store: Session) -> tuple[CallContext, SecretStr] | VoiceSessionView:
                VoiceService(store, self).human(ctx, Permission.INVESTIGATE)
                row = store.get(VoiceSession, key, with_for_update=True)
                if row is None or row.tenant_id != ctx.tenant_id:
                    raise HTTPException(404, "VOICE_SESSION_NOT_FOUND")
                grant = store.get(VoiceLiveGrant, self.authorization.grant_id)
                if grant is None or ctx.actor_id not in {row.initiator_id, grant.participant_id}:
                    raise HTTPException(403, "VOICE_LIVE_STOP_SCOPE_DENIED")
                if self.stop_attempted(store, row):
                    return VoiceService(store, self).view(row)
                if row.version != expected_version:
                    raise HTTPException(409, "VOICE_VERSION_CONFLICT")
                self.stop_current(store, self.context(row))
                _, material = self.private_material(store, row)
                if not isinstance(material.get("control"), str):
                    raise HTTPException(409, "VOICE_LIVE_STOP_UNAVAILABLE")
                control = material["control"]
                assert isinstance(control, str)
                return self.context(row), SecretStr(control)

            staged = await db.run_sync(stage)
        if isinstance(staged, VoiceSessionView):
            return staged
        call, control = staged
        try:
            await self.port(call.tenant_id, call.connection_id).end_call(call, control)
        except VoiceFailure as exc:
            raise HTTPException(409, "VOICE_LIVE_STOP_OUTCOME_UNKNOWN") from exc
        async with self.sessions.begin() as db:

            def view(store: Session) -> VoiceSessionView:
                row = store.get(VoiceSession, key)
                assert row is not None
                return VoiceService(store, self).view(row)

            return await db.run_sync(view)
