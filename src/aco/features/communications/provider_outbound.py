"""Server-selected outbound provider composition; credentials never confer send authority."""

import json
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import Field, ValidationError, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from aco.core.context import ServiceContext, TenantContext
from aco.core.settings import Settings
from aco.core.types import ProviderErrorCode, ProviderFailure, StrictModel
from aco.features.communications.dto import EmailExecutionView
from aco.features.communications.email_live import (
    EmailLiveAuthorization,
    config_for,
    current_email_live_grant,
)
from aco.features.communications.models import (
    EmailActivationRevocation,
    EmailDispatchBinding,
    EmailExecutionActivation,
    EmailSubmission,
)
from aco.features.communications.provider_ingress import (
    Authority,
    EmailContext,
    ProviderIngressRuntime,
    persisted_capability,
)
from aco.features.identity.models import Membership, ProviderConnection, Tenant, now
from aco.features.identity.service import IdentityService, Permission
from aco.features.policies.dto import DispatchTicket, OutboundProposal
from aco.features.policies.models import DispatchTicketRecord
from aco.integrations.ports.email import EmailFailure, EmailPort
from aco.integrations.ports.email_dto import OutboundEmail, mailbox
from aco.integrations.ports.email_observer import EmailRequestObserver


class ProviderOutboundCapability(StrictModel):
    tenant_id: UUID
    connection_id: UUID
    provider: Literal["resend"] = "resend"
    credential_revision: str = Field(min_length=1, max_length=200)
    account_id: str = Field(min_length=1, max_length=200)
    mode: Literal["ROUNDTRIP_TEST", "OUTBOUND_TEST_ONLY"] = "ROUNDTRIP_TEST"
    ingress_activation_id: UUID | None
    sender: str
    destinations: tuple[str, ...] = Field(min_length=1, max_length=100)
    request_cap: int = Field(ge=1, le=100)
    observe_sent_identity: bool = False
    evidence_mode: Literal["MOCKED_HTTP", "LIVE"] = "MOCKED_HTTP"
    live_grant_id: UUID | None = None

    @model_validator(mode="after")
    def scoped_mode(self) -> "ProviderOutboundCapability":
        if self.mode == "OUTBOUND_TEST_ONLY":
            if (
                self.evidence_mode != "LIVE"
                or self.live_grant_id is None
                or self.ingress_activation_id is not None
            ):
                raise ValueError("Explicit outbound-only live grant without ingress required")
        elif self.ingress_activation_id is None:
            raise ValueError("Round-trip mode requires current ingress")
        return self

    @field_validator("sender")
    @classmethod
    def sender_address(cls, value: str) -> str:
        return mailbox(value).casefold()

    @field_validator("destinations")
    @classmethod
    def destination_addresses(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(mailbox(value).casefold() for value in values)))


class ReviewProviderOutbound(StrictModel):
    connection_id: Annotated[UUID, Field(strict=False)]
    expected_activation_id: Annotated[UUID, Field(strict=False)] | None
    evidence: str = Field(min_length=1, max_length=1000)


PortFactory = Callable[[ProviderOutboundCapability, Authority, EmailRequestObserver], EmailPort]
CapabilityResolver = Callable[[UUID, UUID], ProviderOutboundCapability]


def persisted_outbound(db: Session, row: EmailExecutionActivation) -> ProviderOutboundCapability:
    from aco.features.communications.ingress import current_activation

    try:
        cap = ProviderOutboundCapability.model_validate_json(json.dumps(row.provider_binding))
    except (ValidationError, TypeError, ValueError):
        raise HTTPException(403, "PROVIDER_OUTBOUND_BINDING_INVALID") from None
    if cap.evidence_mode == "LIVE":
        validate_live_material(db, cap)
    elif cap.live_grant_id is not None:
        raise HTTPException(403, "PROVIDER_OUTBOUND_BINDING_INVALID")
    connection = db.get(ProviderConnection, cap.connection_id, populate_existing=True)
    if (
        connection is None
        or connection.external_account_id != cap.account_id
        or (cap.tenant_id, cap.connection_id) != (row.tenant_id, row.email_connection_id)
        or cap.sender != row.sender
        or list(cap.destinations) != row.destinations
        or cap.request_cap != row.request_cap
    ):
        raise HTTPException(403, "PROVIDER_OUTBOUND_BINDING_INVALID")
    if cap.mode == "OUTBOUND_TEST_ONLY":
        assert cap.live_grant_id is not None
        grant = current_email_live_grant(db, cap.live_grant_id, cap.tenant_id, cap.connection_id)
        if (row.storage_connection_id, row.storage_connection_version, row.capture_policy) != (
            grant.storage_connection_id,
            grant.storage_connection_version,
            "approved_email_test",
        ):
            raise HTTPException(403, "PROVIDER_OUTBOUND_BINDING_INVALID")
    else:
        assert cap.ingress_activation_id is not None
        inbound = current_activation(db, cap.ingress_activation_id, row.tenant_id)
        captured = persisted_capability(db, inbound)
        if (
            captured.evidence_mode != cap.evidence_mode
            or (cap.evidence_mode == "LIVE" and captured.live_grant_id != cap.live_grant_id)
            or captured.connection_id != cap.connection_id
            or captured.credential_revision != cap.credential_revision
            or cap.sender not in captured.inbox_addresses
            or inbound.storage_connection_id != row.storage_connection_id
            or inbound.storage_connection_version != row.storage_connection_version
        ):
            raise HTTPException(403, "PROVIDER_OUTBOUND_BINDING_INVALID")
    return cap


def validate_live_material(db: Session, cap: ProviderOutboundCapability) -> None:
    if cap.live_grant_id is None:
        raise HTTPException(403, "REAL_EMAIL_SEND_NOT_AUTHORIZED")
    grant = current_email_live_grant(db, cap.live_grant_id, cap.tenant_id, cap.connection_id)
    config = config_for(grant)
    if (
        cap.mode != config.mode
        or cap.sender != config.sender
        or cap.destinations != (config.recipient,)
        or cap.account_id != grant.account_id
        or cap.credential_revision != grant.credential_revision
        or cap.request_cap > config.max_messages
    ):
        raise HTTPException(403, "EMAIL_LIVE_SCOPE_CHANGED")


class WireObservation:
    """Shares the canonical EmailSubmission attempt; does not create a second usage ledger."""

    def __init__(
        self,
        authorize: Authority,
        reserve: Callable[[EmailContext, str], Awaitable[None]] | None = None,
        *,
        synthetic: bool = True,
    ):
        self.authorize, self.reserve, self.synthetic = authorize, reserve, synthetic
        self.started_wire = False
        self.provider_request_id: str | None = None

    async def started(self, ctx: EmailContext, operation: str, material: object | None, /) -> object:
        await self.authorize(ctx, operation, material)
        if self.started_wire:
            raise EmailFailure(
                ProviderFailure(
                    code=ProviderErrorCode.UNKNOWN_SIDE_EFFECT, retryable=False, may_have_side_effect=True
                )
            )
        if self.reserve is not None:
            await self.reserve(ctx, operation)
            await self.authorize(ctx, operation, material)
        self.started_wire = True
        return self

    async def finished(
        self,
        token: object,
        failure: ProviderFailure | None,
        provider_request_id: str | None,
        status_code: int | None,
        response_bytes: int,
        /,
    ) -> None:
        assert token is self
        self.provider_request_id = provider_request_id


class ProviderOutboundRuntime:
    provider = "resend"

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        ingress_runtime: ProviderIngressRuntime | None,
        capability_resolver: CapabilityResolver,
        port_factory: PortFactory,
        *,
        enabled: bool = False,
        live_authorization: EmailLiveAuthorization | None = None,
    ):
        self.sessions, self.settings, self.ingress_runtime = sessions, settings, ingress_runtime
        self.resolve, self.port_factory, self.enabled = capability_resolver, port_factory, enabled
        self.live_authorization = live_authorization

    def available(self) -> bool:
        return (
            self.enabled
            and self.settings.app_env in {"local", "demo"}
            and self.settings.outbound_enabled
            and not self.settings.kill_switch
            and self.settings.external_providers == "fake"
            and (self.ingress_runtime is not None or self.live_authorization is not None)
        )

    def capability(self, tenant_id: UUID, connection_id: UUID) -> ProviderOutboundCapability:
        if not self.available():
            raise HTTPException(403, "PROVIDER_OUTBOUND_DISABLED")
        try:
            cap = self.resolve(tenant_id, connection_id)
        except LookupError:
            raise HTTPException(403, "PROVIDER_OUTBOUND_BINDING_UNAVAILABLE") from None
        if not isinstance(cap, ProviderOutboundCapability) or (cap.tenant_id, cap.connection_id) != (
            tenant_id,
            connection_id,
        ):
            raise HTTPException(403, "PROVIDER_OUTBOUND_BINDING_INVALID")
        if cap.evidence_mode == "LIVE" and (
            self.live_authorization is None or cap.live_grant_id != self.live_authorization.grant_id
        ):
            raise HTTPException(403, "REAL_EMAIL_SEND_NOT_AUTHORIZED")
        return cap

    def validate(self, db: Session, row: EmailExecutionActivation) -> ProviderOutboundCapability:
        from aco.features.communications.activation import validate_activation
        from aco.features.communications.ingress import current_activation

        cap = self.capability(row.tenant_id, row.email_connection_id)
        if cap.evidence_mode == "LIVE":
            assert self.live_authorization is not None
            self.live_authorization.validate(db, cap.tenant_id, cap.connection_id)
        tenant = db.get(Tenant, row.tenant_id, populate_existing=True)
        if tenant is None:
            raise HTTPException(403, "PROVIDER_OUTBOUND_BINDING_INVALID")
        validate_activation(db, tenant, row)
        if cap != persisted_outbound(db, row):
            raise HTTPException(403, "PROVIDER_OUTBOUND_CONFIGURATION_CHANGED")
        if cap.mode == "ROUNDTRIP_TEST":
            if self.ingress_runtime is None or cap.ingress_activation_id is None:
                raise HTTPException(403, "PROVIDER_OUTBOUND_BINDING_INVALID")
            self.ingress_runtime.validate(
                db, current_activation(db, cap.ingress_activation_id, cap.tenant_id)
            )
        return cap

    def activate(
        self, db: Session, ctx: TenantContext, command: ReviewProviderOutbound
    ) -> EmailExecutionView:
        from aco.features.communications.activation import EmailActivationService, latest
        from aco.features.communications.ingress import current_activation

        if not isinstance(ctx, TenantContext):
            raise HTTPException(403, "HUMAN_REQUIRED")
        db.get(Tenant, ctx.tenant_id, populate_existing=True, with_for_update={"key_share": True})
        tenant = IdentityService(db).authorize(ctx, Permission.SETTINGS)
        cap = self.capability(ctx.tenant_id, command.connection_id)
        if cap.mode == "OUTBOUND_TEST_ONLY":
            assert self.live_authorization is not None
            grant = self.live_authorization.validate(db, cap.tenant_id, cap.connection_id)
            validate_live_material(db, cap)
            email_version = grant.connection_version
            storage_id, storage_version = grant.storage_connection_id, grant.storage_connection_version
            expires_at = config_for(grant).expires_at
        else:
            if self.ingress_runtime is None or cap.ingress_activation_id is None:
                raise HTTPException(403, "PROVIDER_OUTBOUND_BINDING_INVALID")
            inbound = current_activation(db, cap.ingress_activation_id, tenant.id)
            source = self.ingress_runtime.validate(db, inbound)
            if (source.connection_id, source.credential_revision) != (
                cap.connection_id,
                cap.credential_revision,
            ) or cap.sender not in source.inbox_addresses:
                raise HTTPException(403, "PROVIDER_OUTBOUND_BINDING_INVALID")
            email_version = inbound.email_connection_version
            storage_id, storage_version = inbound.storage_connection_id, inbound.storage_connection_version
            expires_at = inbound.expires_at
        prior = latest(db, tenant.id)
        if (prior.id if prior else None) != command.expected_activation_id:
            raise HTTPException(409, "EMAIL_ACTIVATION_CHANGED")
        member = db.scalar(
            select(Membership).where(Membership.tenant_id == tenant.id, Membership.user_id == ctx.actor_id)
        )
        assert member is not None
        if prior and not db.scalar(
            select(EmailActivationRevocation.id).where(EmailActivationRevocation.activation_id == prior.id)
        ):
            db.add(
                EmailActivationRevocation(
                    tenant_id=tenant.id,
                    activation_id=prior.id,
                    actor_id=ctx.actor_id,
                    reason="superseded_by_provider_review",
                )
            )
        row = EmailExecutionActivation(
            tenant_id=tenant.id,
            email_connection_id=cap.connection_id,
            email_connection_version=email_version,
            storage_connection_id=storage_id,
            storage_connection_version=storage_version,
            reviewer_id=ctx.actor_id,
            membership_version=member.version,
            sender=cap.sender,
            destinations=list(cap.destinations),
            request_cap=cap.request_cap,
            capture_policy="approved_email_test" if cap.evidence_mode == "LIVE" else "synthetic_30_days",
            evidence=command.evidence,
            expires_at=min(now() + timedelta(days=1), expires_at),
            provider_binding=cap.model_dump(mode="json"),
        )
        db.add(row)
        db.flush()
        self.validate(db, row)
        IdentityService(db).audit(
            ctx,
            "email.provider_execution_reviewed",
            row.id,
            {"evidence_mode": cap.evidence_mode, "synthetic": cap.evidence_mode == "MOCKED_HTTP"},
        )
        return EmailActivationService(db, self.settings, self).view(ctx)

    async def port(
        self,
        db: AsyncSession,
        ctx: ServiceContext,
        binding_id: UUID,
        ticket: DispatchTicket,
        proposal: OutboundProposal,
    ) -> tuple[EmailPort, WireObservation]:
        from aco.features.policies.guards import PROCESS_INCARNATION, PolicyGuards

        binding = await db.get(EmailDispatchBinding, binding_id, populate_existing=True)
        if binding is None or (binding.tenant_id, binding.action_id) != (ctx.tenant_id, ctx.action_id):
            raise HTTPException(403, "PROVIDER_OUTBOUND_SCOPE")
        row = await db.get(EmailExecutionActivation, binding.activation_id)
        assert row is not None
        cap = await db.run_sync(lambda store: self.validate(store, row))
        expected = OutboundEmail.model_validate_json(json.dumps(binding.command))

        async def authorize(actual: EmailContext, operation: str, material: object | None, /) -> None:
            try:
                if (
                    actual != ctx
                    or not isinstance(actual, ServiceContext)
                    or operation != "send_email"
                    or material != expected
                ):
                    raise HTTPException(403, "PROVIDER_OUTBOUND_SCOPE")

                def check(store: Session) -> None:
                    IdentityService(store).authorize(ctx, Permission.INVOICE_READ)
                    self.validate(store, row)
                    saved = store.get(DispatchTicketRecord, ticket.id, populate_existing=True)
                    submission = store.scalar(
                        select(EmailSubmission)
                        .where(EmailSubmission.binding_id == binding.id)
                        .execution_options(populate_existing=True)
                    )
                    if (
                        saved is None
                        or (saved.action_id, saved.attempt_id, saved.read_id, saved.incarnation)
                        != (ctx.action_id, proposal.attempt_id, proposal.read_id, PROCESS_INCARNATION)
                        or saved.consumed_at is None
                        or submission is None
                        or submission.state != "pending"
                        or submission.usage_attempt_id is None
                    ):
                        raise HTTPException(403, "PROVIDER_OUTBOUND_INTENT_REQUIRED")
                    guard = PolicyGuards(
                        store, now(), PROCESS_INCARNATION, settings=self.settings, email_runtime=self
                    )
                    if saved.bindings != guard._bindings(ctx, proposal) or not timedelta(
                        0
                    ) <= now() - saved.issued_at < timedelta(seconds=60):
                        raise HTTPException(403, "PROVIDER_OUTBOUND_TICKET_CHANGED")
                    from aco.features.communications.outbound import dispatch_prerequisites

                    reasons = dispatch_prerequisites(
                        store, ctx, proposal, self.settings, self, pending_attempt=submission.usage_attempt_id
                    )
                    if reasons:
                        raise HTTPException(403, reasons[0])
                    decision = guard.evaluate_outbound(ctx, proposal, now())
                    if any(reason != "EMAIL_OPERATION_FENCED" for reason in decision.reasons):
                        raise HTTPException(403, "PROVIDER_OUTBOUND_GUARD_CHANGED")

                await db.run_sync(check)
            except HTTPException as error:
                raise EmailFailure(
                    ProviderFailure(
                        code=ProviderErrorCode.AUTH_EXPIRED, retryable=False, may_have_side_effect=False
                    )
                ) from error

        async def reserve(actual: EmailContext, operation: str) -> None:
            live = self.live_authorization
            assert live is not None
            async with self.sessions.begin() as evidence:

                def reserve_attempt(store: Session) -> None:
                    submission = store.scalar(
                        select(EmailSubmission).where(
                            EmailSubmission.binding_id == binding.id,
                            EmailSubmission.tenant_id == actual.tenant_id,
                        )
                    )
                    if (
                        submission is None
                        or submission.usage_attempt_id is None
                        or submission.state != "pending"
                    ):
                        raise HTTPException(403, "PROVIDER_OUTBOUND_INTENT_REQUIRED")
                    live.reserve(
                        store, actual, cap.connection_id, operation, attempt_id=submission.usage_attempt_id
                    )

                await evidence.run_sync(reserve_attempt)

        observation = WireObservation(
            authorize,
            reserve if cap.evidence_mode == "LIVE" else None,
            synthetic=cap.evidence_mode == "MOCKED_HTTP",
        )
        port = self.port_factory(cap, authorize, observation)
        return port, observation
