"""Explicit server-only inbound capability; no credential or generic worker promotion."""

import json
from collections.abc import Callable
from datetime import timedelta
from typing import Literal, Protocol
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import Field, ValidationError, model_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from aco.core.context import ActorContext, TenantContext
from aco.core.types import CostQuality, ProviderCost, ProviderFailure, ProviderReference, StrictModel
from aco.features.artifacts.models import RetentionPolicy
from aco.features.communications.email_live import (
    EmailLiveAuthorization,
    config_for,
    current_email_live_grant,
)
from aco.features.communications.ingress_dto import ActivateIngress, IngressActivationView
from aco.features.communications.models import EmailIngressActivation
from aco.features.identity.models import Actor, Membership, ProviderConnection, Tenant, now
from aco.features.identity.service import IdentityService, Permission
from aco.features.usage.dto import ProviderUsageInput, Units
from aco.features.usage.models import UsageAttempt, UsageSource
from aco.features.usage.service import UsageService
from aco.integrations.ports.email import EmailPort
from aco.integrations.ports.email_context import EmailIngressContext, EmailWebhookContext
from aco.integrations.ports.email_observer import EmailRequestObserver

EmailContext = ActorContext | EmailIngressContext | EmailWebhookContext


class Authority(Protocol):
    async def __call__(self, ctx: EmailContext, operation: str, material: object | None, /) -> None: ...


class ProviderIngressCapability(StrictModel):
    """Public material resolved by the control plane, never supplied as HTTP authority."""

    tenant_id: UUID
    connection_id: UUID
    provider: Literal["resend"] = "resend"
    credential_revision: str = Field(min_length=1, max_length=200)
    inbox_addresses: tuple[str, ...] = Field(min_length=1, max_length=8)
    webhook_secret_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    retention_policy_id: UUID
    retention_policy_version: int = Field(ge=1)
    capture_purpose: Literal["offline_provider_original_fixture", "approved_live_test_capture"] = (
        "offline_provider_original_fixture"
    )
    evidence_mode: Literal["MOCKED_HTTP", "LIVE"] = "MOCKED_HTTP"
    live_grant_id: UUID | None = None

    @model_validator(mode="after")
    def explicit_lane(self) -> "ProviderIngressCapability":
        if self.evidence_mode == "LIVE":
            if self.live_grant_id is None or self.capture_purpose != "approved_live_test_capture":
                raise ValueError("Explicit live grant and approved test capture required")
        elif self.live_grant_id is not None or self.capture_purpose != "offline_provider_original_fixture":
            raise ValueError("Mocked capture cannot carry live authority")
        return self


PortFactory = Callable[[ProviderIngressCapability, Authority, EmailRequestObserver], EmailPort]
CapabilityResolver = Callable[[UUID, UUID], ProviderIngressCapability]


def persisted_capability(db: Session, row: EmailIngressActivation) -> ProviderIngressCapability:
    """Current persisted material check, also used by later source/agent reads."""
    try:
        capability = ProviderIngressCapability.model_validate_json(json.dumps(row.provider_binding))
    except (ValidationError, TypeError, ValueError):
        raise HTTPException(403, "PROVIDER_INGRESS_BINDING_INVALID") from None
    validate_material(db, capability)
    from aco.features.communications.mailboxes import validate_mailbox_claims

    validate_mailbox_claims(db, row, capability.inbox_addresses)
    if (
        capability.tenant_id != row.tenant_id
        or capability.connection_id != row.email_connection_id
        or capability.webhook_secret_sha256 != row.secret_sha256
    ):
        raise HTTPException(403, "PROVIDER_INGRESS_BINDING_INVALID")
    return capability


def validate_material(db: Session, capability: ProviderIngressCapability) -> None:
    connection = db.get(ProviderConnection, capability.connection_id, populate_existing=True)
    policy = db.get(RetentionPolicy, capability.retention_policy_id, populate_existing=True)
    if (
        connection is None
        or connection.tenant_id != capability.tenant_id
        or connection.provider != capability.provider
        or connection.provider_type != "email"
        or connection.status != "READY"
        or connection.encrypted_credentials_ref != capability.credential_revision
        or policy is None
        or policy.tenant_id != capability.tenant_id
        or policy.version != capability.retention_policy_version
        or any("@" not in item or item != item.strip().lower() for item in capability.inbox_addresses)
    ):
        raise HTTPException(403, "PROVIDER_INGRESS_MATERIAL_REVOKED")
    if capability.evidence_mode == "LIVE":
        if capability.live_grant_id is None:
            raise HTTPException(403, "REAL_EMAIL_CAPTURE_NOT_AUTHORIZED")
        grant = current_email_live_grant(
            db, capability.live_grant_id, capability.tenant_id, capability.connection_id
        )
        approved = config_for(grant)
        if (
            capability.capture_purpose != "approved_live_test_capture"
            or capability.inbox_addresses != (approved.reply_mailbox,)
            or capability.credential_revision != grant.credential_revision
            or (capability.retention_policy_id, capability.retention_policy_version)
            != (grant.retention_policy_id, grant.retention_policy_version)
        ):
            raise HTTPException(403, "PROVIDER_INGRESS_LIVE_SCOPE")
    elif (
        policy.policy_key != "synthetic_30_days"
        or not policy.synthetic
        or policy.retention_days != 30
        or policy.authority != "synthetic_only_no_real_capture_authority"
    ):
        raise HTTPException(403, "PROVIDER_INGRESS_MATERIAL_REVOKED")


class ProviderIngressRuntime:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        capability_resolver: CapabilityResolver,
        port_factory: PortFactory,
        *,
        enabled: bool = False,
        environment: str = "local",
        live_authorization: EmailLiveAuthorization | None = None,
    ):
        self.sessions, self.resolve, self.port_factory = sessions, capability_resolver, port_factory
        self.enabled, self.environment = enabled, environment
        self.live_authorization = live_authorization

    def capability(self, tenant_id: UUID, connection_id: UUID) -> ProviderIngressCapability:
        if not self.enabled or self.environment not in {"local", "demo"}:
            raise HTTPException(403, "PROVIDER_INGRESS_DISABLED")
        try:
            cap = self.resolve(tenant_id, connection_id)
        except LookupError:
            raise HTTPException(403, "PROVIDER_INGRESS_BINDING_UNAVAILABLE") from None
        if not isinstance(cap, ProviderIngressCapability) or (cap.tenant_id, cap.connection_id) != (
            tenant_id,
            connection_id,
        ):
            raise HTTPException(403, "PROVIDER_INGRESS_BINDING_INVALID")
        if cap.evidence_mode == "LIVE" and (
            self.live_authorization is None or cap.live_grant_id != self.live_authorization.grant_id
        ):
            raise HTTPException(403, "REAL_EMAIL_CAPTURE_NOT_AUTHORIZED")
        return cap

    def validate(self, db: Session, row: EmailIngressActivation) -> ProviderIngressCapability:
        cap = self.capability(row.tenant_id, row.email_connection_id)
        if cap != persisted_capability(db, row):
            raise HTTPException(403, "PROVIDER_INGRESS_CONFIGURATION_CHANGED")
        if cap.evidence_mode == "LIVE":
            assert self.live_authorization is not None
            grant = self.live_authorization.validate(db, row.tenant_id, row.email_connection_id)
            if row.storage_connection_id != grant.storage_connection_id:
                raise HTTPException(403, "PROVIDER_INGRESS_LIVE_SCOPE")
        return cap

    def activate(self, db: Session, ctx: TenantContext, command: ActivateIngress) -> IngressActivationView:
        if not isinstance(ctx, TenantContext):
            raise HTTPException(403, "HUMAN_REQUIRED")
        db.get(Tenant, ctx.tenant_id, populate_existing=True, with_for_update={"key_share": True})
        tenant = IdentityService(db).authorize(ctx, Permission.SETTINGS)
        cap = self.capability(ctx.tenant_id, command.email_connection_id)
        validate_material(db, cap)
        expires_at = now() + timedelta(seconds=command.expires_in_seconds)
        if cap.evidence_mode == "LIVE":
            assert self.live_authorization is not None
            grant = self.live_authorization.validate(db, ctx.tenant_id, cap.connection_id)
            if (
                command.storage_connection_id != grant.storage_connection_id
                or ctx.actor_id != grant.initiator_id
            ):
                raise HTTPException(403, "PROVIDER_INGRESS_LIVE_SCOPE")
            expires_at = min(expires_at, config_for(grant).expires_at)
        email = db.get(ProviderConnection, command.email_connection_id)
        storage = db.get(ProviderConnection, command.storage_connection_id)
        if (
            not tenant.is_demo
            or storage is None
            or storage.tenant_id != tenant.id
            or storage.provider_type != "storage"
            or storage.status != "READY"
        ):
            raise HTTPException(403, "INGRESS_CONNECTION_INVALID")
        assert email is not None
        member = db.scalar(
            select(Membership).where(Membership.tenant_id == tenant.id, Membership.user_id == ctx.actor_id)
        )
        assert member is not None
        actor = db.scalar(
            select(Actor).where(Actor.tenant_id == tenant.id, Actor.purpose == "durable_worker")
        )
        if actor is None:
            actor = Actor(kind="service", tenant_id=tenant.id, purpose="durable_worker", active=True)
            db.add(actor)
            db.flush()
        if not actor.active:
            raise HTTPException(403, "INGRESS_WORKER_REVOKED")
        row = EmailIngressActivation(
            tenant_id=tenant.id,
            email_connection_id=email.id,
            email_connection_version=email.version,
            storage_connection_id=storage.id,
            storage_connection_version=storage.version,
            enabling_member_id=ctx.actor_id,
            membership_version=member.version,
            worker_actor_id=actor.id,
            actor_version=actor.version,
            tenant_generation=tenant.version,
            secret_sha256=cap.webhook_secret_sha256,
            evidence=command.evidence,
            expires_at=expires_at,
            provider_binding=cap.model_dump(mode="json"),
        )
        db.add(row)
        db.flush()
        from aco.features.communications.mailboxes import claim_mailboxes

        claim_mailboxes(db, row, cap.inbox_addresses)
        IdentityService(db).audit(
            ctx,
            "email.provider_ingress_activated",
            row.id,
            {
                "synthetic": cap.evidence_mode == "MOCKED_HTTP",
                "evidence_mode": cap.evidence_mode,
                "provider": cap.provider,
            },
        )
        return IngressActivationView.model_validate(row, from_attributes=True).model_copy(
            update={"synthetic": cap.evidence_mode == "MOCKED_HTTP"}
        )

    async def authorize(self, ctx: EmailContext, operation: str, material: object) -> None:
        from aco.features.communications.ingress import current_activation, current_receipt

        async with self.sessions.begin() as db:

            def check(store: Session) -> None:
                if not isinstance(ctx, (EmailIngressContext, EmailWebhookContext)):
                    raise HTTPException(403, "PROVIDER_INGRESS_CONTEXT_REQUIRED")
                row = current_activation(store, ctx.activation_id, ctx.tenant_id)
                self.validate(store, row)
                if ctx.actor_id != row.worker_actor_id or ctx.email_connection_id != row.email_connection_id:
                    raise HTTPException(403, "PROVIDER_INGRESS_CONTEXT_REQUIRED")
                if isinstance(ctx, EmailWebhookContext):
                    if operation != "verify_webhook" or material != ctx.raw_sha256:
                        raise HTTPException(403, "PROVIDER_INGRESS_WEBHOOK_SCOPE")
                else:
                    current_receipt(store, ctx)
                    if operation not in {"get_message", "download_original"} or material != ProviderReference(
                        provider="resend", connection_id=ctx.email_connection_id, external_id=ctx.message_id
                    ):
                        raise HTTPException(403, "PROVIDER_INGRESS_READ_SCOPE")

            await db.run_sync(check)

    async def archive_authority(self, ctx: EmailIngressContext) -> None:
        await self.authorize(
            ctx,
            "get_message",
            ProviderReference(
                provider="resend", connection_id=ctx.email_connection_id, external_id=ctx.message_id
            ),
        )

    def port(self, row: EmailIngressActivation, db: Session) -> EmailPort:
        return self.port_factory(self.validate(db, row), self.authorize, self)

    async def started(self, ctx: EmailContext, operation: str, material: object) -> object:
        # Recheck after the adapter's authorization callback and durably retain intent before wire.
        await self.authorize(ctx, operation, material)
        assert isinstance(ctx, EmailIngressContext)
        key = uuid4()
        async with self.sessions.begin() as db:
            from aco.features.communications.ingress import current_activation

            cap = await db.run_sync(
                lambda store: self.validate(
                    store, current_activation(store, ctx.activation_id, ctx.tenant_id)
                )
            )
            db.add(
                UsageAttempt(
                    id=key,
                    tenant_id=ctx.tenant_id,
                    provider_connection_id=ctx.email_connection_id,
                    actor_id=ctx.actor_id,
                    correlation_id=ctx.correlation_id,
                    operation=f"email_ingress_{operation}",
                    outcome="ATTEMPTED",
                    business_outcome="PENDING",
                )
            )
            await db.flush()
            if cap.evidence_mode == "LIVE":
                authorization = self.live_authorization
                assert authorization is not None
                await db.run_sync(
                    lambda store: authorization.reserve(
                        store, ctx, ctx.email_connection_id, operation, attempt_id=key
                    )
                )
        # The intent commit is not authority to send after config/kill changed.
        await self.authorize(ctx, operation, material)
        return (key, ctx, cap.evidence_mode == "MOCKED_HTTP")

    async def finished(
        self,
        token: object,
        failure: ProviderFailure | None,
        provider_request_id: str | None,
        status_code: int | None,
        response_bytes: int,
    ) -> None:
        # This is evidence completion, not renewed authority: preserve wire outcomes after revocation.
        assert isinstance(token, tuple) and len(token) == 3
        key, ctx, synthetic = token
        assert isinstance(synthetic, bool)
        assert isinstance(key, UUID) and isinstance(ctx, EmailIngressContext)
        async with self.sessions.begin() as db:
            row = await db.get(UsageAttempt, key, with_for_update=True)
            assert row is not None and row.tenant_id == ctx.tenant_id and row.event_id is None
            outcome = "VERIFIED" if failure is None else "FAILED"

            def record(store: Session) -> UUID:
                event = UsageService(store).record_verified(
                    ctx,
                    ProviderUsageInput(
                        provider_connection_id=ctx.email_connection_id,
                        operation_key=str(key),
                        component="email_http_request",
                        operation=row.operation,
                        provider_kind="email",
                        provider="resend",
                        synthetic=synthetic,
                        cost=ProviderCost(amount=None, currency="USD", quality=CostQuality.UNKNOWN),
                        units=Units(requests=1),
                        native_operation_id=provider_request_id,
                        outcome=outcome,
                    ),
                )
                store.add(
                    UsageSource(
                        event_id=event.id,
                        source_kind="email_ingress_receipt",
                        source_id=ctx.receipt_id,
                        component=str(key),
                    )
                )
                return event.id

            row.event_id = await db.run_sync(record)
            row.outcome = outcome
            row.error_reference = failure.code.value if failure else None


def record_capture_outcome(
    db: Session, ctx: EmailIngressContext, outcome: Literal["COMMITTED", "FAILED"]
) -> None:
    """Same transaction as canonical receipt outcome; wire evidence survives capture rollback."""
    events = select(UsageSource.event_id).where(
        UsageSource.source_kind == "email_ingress_receipt", UsageSource.source_id == ctx.receipt_id
    )
    db.execute(
        update(UsageAttempt)
        .where(
            UsageAttempt.tenant_id == ctx.tenant_id,
            UsageAttempt.provider_connection_id == ctx.email_connection_id,
            UsageAttempt.event_id.in_(events),
            UsageAttempt.business_outcome == "PENDING",
        )
        .values(business_outcome=outcome)
    )
