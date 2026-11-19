"""Explicit operator-installed browser-voice authority; no credentials, HTTP installer or calls.

This owns immutable authority links only. Canonical BudgetReservation/UsageAttempt remain
all exposure and cost truth; unmeasured provider bills stay UNKNOWN, including HTTP success.
"""

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from ipaddress import ip_address
from uuid import UUID

from fastapi import HTTPException
from pydantic import AwareDatetime, Field, field_validator, model_validator
from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
    select,
    text,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from aco.core.context import TenantContext
from aco.core.live import APPROVED_LIVE_AUTHORIZATIONS
from aco.core.types import Role, StrictModel
from aco.features.accounting.models import InvoiceSnapshot
from aco.features.identity.models import (
    Actor,
    AuditEvent,
    Base,
    Membership,
    ProviderConnection,
    Record,
    Tenant,
    User,
    now,
)
from aco.features.identity.service import ROLE_PERMISSIONS, IdentityService, Permission
from aco.features.policies.dto import PolicyDefinition
from aco.features.policies.models import CollectionPolicy, CollectionPolicyVersion
from aco.features.usage.budgets import BudgetService
from aco.features.usage.dto import Units, UsageReservationRequest
from aco.features.usage.live_scopes import live_provider_scope
from aco.features.usage.models import (
    BudgetLimit,
    BudgetReservation,
    BudgetScope,
    ReservationOutcome,
    UsageAttempt,
)
from aco.features.usage.service import UsageContext
from aco.integrations.ports.email_dto import mailbox

OPERATIONS = frozenset({"probe_assistant", "probe_credential", "start_browser", "get_call", "end_call"})


class VoiceLiveConfig(StrictModel):
    authorization_ref: str = Field(min_length=1, max_length=200)
    source_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    account_id: str = Field(min_length=1, max_length=200)
    assistant_id: UUID
    binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    participant_email: str
    participant_approval_ref: str = Field(min_length=10, max_length=1000)
    participant_timezone: str = Field(min_length=1, max_length=100)
    participant_mode: str = Field(
        default="LIVE_HUMAN_MICROPHONE_CAPTURE_OFF", pattern=r"^LIVE_HUMAN_MICROPHONE_CAPTURE_OFF$"
    )
    no_capture_approval_ref: str = Field(min_length=10, max_length=1000)
    callback_url: str = Field(min_length=1, max_length=1000)
    server_credential_id: UUID
    assistant_profile_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_version_id: UUID
    invoice_ids: list[UUID] = Field(min_length=1, max_length=3)
    expires_at: AwareDatetime
    max_calls: int = Field(ge=1, le=3)
    max_call_seconds: int = Field(ge=1, le=300)
    max_total_seconds: int = Field(ge=1, le=900)
    max_requests: int = Field(ge=6, le=20)
    max_usd: Decimal = Field(gt=0, le=Decimal("20"), decimal_places=12)
    all_component_usd_per_second: Decimal = Field(gt=0, le=Decimal("1"), decimal_places=12)
    api_request_usd_ceiling: Decimal = Field(gt=0, le=Decimal("1"), decimal_places=12)
    price_source_ref: str = Field(min_length=10, max_length=1000)
    price_source_at: AwareDatetime

    @field_validator("participant_email")
    @classmethod
    def participant(cls, value: str) -> str:
        return mailbox(value).casefold()

    @model_validator(mode="after")
    def bounded(self) -> "VoiceLiveConfig":
        from urllib.parse import urlsplit
        from zoneinfo import ZoneInfo

        target = urlsplit(self.callback_url)
        if (
            target.scheme != "https"
            or not target.hostname
            or target.username
            or target.password
            or target.port not in {None, 443}
            or target.query
            or target.fragment
            or target.path != "/api/voice/callback"
            or target.hostname in {"localhost", "127.0.0.1"}
        ):
            raise ValueError("Exact approved HTTPS callback required")
        try:
            address = ip_address(target.hostname or "")
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError("Public callback required")
        if (target.hostname or "").endswith((".localhost", ".local")):
            raise ValueError("Public callback required")
        ZoneInfo(self.participant_timezone)
        if (
            len(set(self.invoice_ids)) != len(self.invoice_ids)
            or self.max_total_seconds > self.max_calls * self.max_call_seconds
            or self.max_requests < 2 * self.max_calls + 4
            or self.max_total_seconds * self.all_component_usd_per_second
            + self.max_requests * self.api_request_usd_ceiling
            > self.max_usd
        ):
            raise ValueError("Call, probe and bounded stop/reconciliation headroom must fit the grant")
        return self


class VoiceLiveGrant(Record, Base):
    __tablename__ = "voice_live_grants"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("authorization_ref"),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"], ["provider_connections.tenant_id", "provider_connections.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "policy_version_id"],
            ["collection_policy_versions.tenant_id", "collection_policy_versions.id"],
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    tenant_generation: Mapped[int]
    initiator_id: Mapped[UUID] = mapped_column(ForeignKey("identity_users.id"))
    participant_id: Mapped[UUID] = mapped_column(ForeignKey("identity_users.id"))
    participant_membership_version: Mapped[int]
    participant_actor_version: Mapped[int]
    membership_version: Mapped[int]
    actor_version: Mapped[int]
    connection_id: Mapped[UUID]
    connection_version: Mapped[int]
    credential_revision: Mapped[str] = mapped_column(Text)
    policy_version_id: Mapped[UUID]
    policy_generation: Mapped[int]
    authorization_ref: Mapped[str] = mapped_column(Text)
    material: Mapped[dict[str, object]] = mapped_column(JSON)
    source_scope: Mapped[list[dict[str, str]]] = mapped_column(JSON)
    scope_ids: Mapped[list[str]] = mapped_column(JSON)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VoiceLiveRequest(Record, Base):
    """Immutable authority/attempt/reservation relation, never a second spend ledger."""

    __tablename__ = "voice_live_requests"
    __table_args__ = (
        UniqueConstraint("attempt_id"),
        CheckConstraint(
            "operation IN ('probe_assistant','probe_credential','start_browser','get_call','end_call')"
        ),
        Index(
            "uq_voice_live_single_create",
            "tenant_id",
            "session_id",
            unique=True,
            postgresql_where=text("operation='start_browser'"),
        ),
        Index(
            "uq_voice_live_single_stop",
            "tenant_id",
            "session_id",
            unique=True,
            postgresql_where=text("operation='end_call'"),
        ),
        ForeignKeyConstraint(
            ["tenant_id", "grant_id"], ["voice_live_grants.tenant_id", "voice_live_grants.id"]
        ),
        ForeignKeyConstraint(["tenant_id", "session_id"], ["voice_sessions.tenant_id", "voice_sessions.id"]),
        ForeignKeyConstraint(["tenant_id", "attempt_id"], ["usage_attempts.tenant_id", "usage_attempts.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "reservation_id"], ["budget_reservations.tenant_id", "budget_reservations.id"]
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    grant_id: Mapped[UUID]
    session_id: Mapped[UUID | None]
    attempt_id: Mapped[UUID]
    reservation_id: Mapped[UUID]
    operation: Mapped[str] = mapped_column(Text)


class VoiceBrowserJoin(Record, Base):
    __tablename__ = "voice_browser_joins"
    __table_args__ = (
        UniqueConstraint("tenant_id", "session_id"),
        ForeignKeyConstraint(
            ["tenant_id", "grant_id"], ["voice_live_grants.tenant_id", "voice_live_grants.id"]
        ),
        ForeignKeyConstraint(["tenant_id", "session_id"], ["voice_sessions.tenant_id", "voice_sessions.id"]),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    grant_id: Mapped[UUID]
    session_id: Mapped[UUID]
    provider_call_id: Mapped[str] = mapped_column(Text)
    encrypted_material: Mapped[bytes] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class VoiceLiveProfile(Record, Base):
    """Actual successful saved assistant and credential reads; no credential values retained."""

    __tablename__ = "voice_live_profiles"
    __table_args__ = (
        UniqueConstraint("grant_id"),
        ForeignKeyConstraint(
            ["tenant_id", "grant_id"], ["voice_live_grants.tenant_id", "voice_live_grants.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "assistant_usage_event_id"],
            ["provider_usage_events.tenant_id", "provider_usage_events.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "credential_usage_event_id"],
            ["provider_usage_events.tenant_id", "provider_usage_events.id"],
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    grant_id: Mapped[UUID]
    connection_version: Mapped[int]
    binding_sha256: Mapped[str] = mapped_column(Text)
    assistant_profile_sha256: Mapped[str] = mapped_column(Text)
    assistant_usage_event_id: Mapped[UUID]
    credential_usage_event_id: Mapped[UUID]


def config_for(grant: VoiceLiveGrant) -> VoiceLiveConfig:
    return VoiceLiveConfig.model_validate_json(json.dumps(grant.material))


def current_grant(
    db: Session, key: UUID, tenant_id: UUID, connection_id: UUID, *, require_ready: bool
) -> VoiceLiveGrant:
    grant = db.get(VoiceLiveGrant, key, populate_existing=True)
    if (
        grant is None
        or (grant.tenant_id, grant.connection_id) != (tenant_id, connection_id)
        or grant.revoked_at
    ):
        raise HTTPException(403, "VOICE_LIVE_GRANT_REQUIRED")
    config = config_for(grant)
    tenant = db.get(Tenant, tenant_id, populate_existing=True)
    member = db.scalar(
        select(Membership)
        .where(Membership.tenant_id == tenant_id, Membership.user_id == grant.initiator_id)
        .execution_options(populate_existing=True)
    )
    actor = db.get(Actor, grant.initiator_id, populate_existing=True)
    connection = db.get(ProviderConnection, connection_id, populate_existing=True)
    participant = db.get(User, grant.participant_id, populate_existing=True)
    participant_actor = db.get(Actor, grant.participant_id, populate_existing=True)
    participant_member = db.scalar(
        select(Membership)
        .where(Membership.tenant_id == tenant_id, Membership.user_id == grant.participant_id)
        .execution_options(populate_existing=True)
    )
    profile = db.scalar(select(VoiceLiveProfile).where(VoiceLiveProfile.grant_id == grant.id))
    expected_connection_version = profile.connection_version if profile else grant.connection_version
    version = db.get(CollectionPolicyVersion, grant.policy_version_id, populate_existing=True)
    policy = db.get(CollectionPolicy, version.policy_id, populate_existing=True) if version else None
    if (
        config.authorization_ref not in APPROVED_LIVE_AUTHORIZATIONS
        or config.expires_at <= now()
        or tenant is None
        or not tenant.is_demo
        or tenant.version != grant.tenant_generation
        or member is None
        or not member.active
        or member.role != "owner"
        or member.version != grant.membership_version
        or actor is None
        or not actor.active
        or actor.kind != "human"
        or actor.version != grant.actor_version
        or participant is None
        or not participant.email_verified
        or participant.email.casefold() != config.participant_email
        or participant_actor is None
        or not participant_actor.active
        or participant_actor.kind != "human"
        or participant_actor.version != grant.participant_actor_version
        or participant_member is None
        or not participant_member.active
        or participant_member.version != grant.participant_membership_version
        or Permission.INVESTIGATE not in ROLE_PERMISSIONS[Role(participant_member.role)]
        or connection is None
        or connection.tenant_id != tenant_id
        or connection.provider != "vapi"
        or connection.provider_type != "voice"
        or connection.external_account_id != config.account_id
        or connection.version != expected_connection_version
        or connection.encrypted_credentials_ref != grant.credential_revision
        or connection.status not in ({"READY"} if require_ready else {"READY", "NOT_CONFIGURED"})
        or version is None
        or version.tenant_id != tenant_id
        or version.state != "ACTIVE"
        or policy is None
        or not policy.active
        or policy.archived_at is not None
        or policy.version != grant.policy_generation
        or version.version != policy.version
        or not PolicyDefinition.model_validate(version.definition).voice_enabled
    ):
        raise HTTPException(403, "VOICE_LIVE_AUTHORITY_CHANGED")
    if require_ready and (
        profile is None
        or profile.binding_sha256 != config.binding_sha256
        or profile.assistant_profile_sha256 != config.assistant_profile_sha256
    ):
        raise HTTPException(403, "VOICE_LIVE_PROFILE_NOT_VERIFIED")
    if require_ready and (tenant.outbound_paused or not tenant.automation_enabled):
        raise HTTPException(403, "VOICE_LIVE_WORKSPACE_PAUSED")
    for scope in grant.source_scope:
        invoice = db.get(InvoiceSnapshot, UUID(scope["invoice_id"]), populate_existing=True)
        account = db.get(ProviderConnection, UUID(scope["accounting_connection_id"]), populate_existing=True)
        if (
            invoice is None
            or invoice.tenant_id != tenant_id
            or not invoice.synthetic
            or not invoice.verified
            or invoice.source_revision != scope["source_revision"]
            or str(invoice.provider_connection_id) != scope["accounting_connection_id"]
            or str(invoice.customer_id) != scope["customer_id"]
            or account is None
            or account.tenant_id != tenant_id
            or account.status != "READY"
            or str(account.version) != scope["accounting_connection_version"]
            or invoice.last_read_at is None
            or now() - invoice.last_read_at > timedelta(minutes=5)
            or invoice.last_read_at > now() + timedelta(seconds=5)
        ):
            raise HTTPException(403, "VOICE_LIVE_SOURCE_CHANGED")
    return grant


def install_voice_live_grant(
    db: Session, ctx: TenantContext, connection_id: UUID, config: VoiceLiveConfig
) -> VoiceLiveGrant:
    """Trusted operator only: no HTTP endpoint, provider request, secret read or automatic activation."""
    config = VoiceLiveConfig.model_validate_json(config.model_dump_json())
    if not isinstance(ctx, TenantContext):
        raise HTTPException(403, "HUMAN_OPERATOR_REQUIRED")
    tenant = IdentityService(db).authorize(ctx, Permission.SETTINGS)
    db.scalar(select(Tenant).where(Tenant.id == tenant.id).with_for_update())
    member = db.scalar(
        select(Membership).where(Membership.tenant_id == ctx.tenant_id, Membership.user_id == ctx.actor_id)
    )
    actor, connection = db.get(Actor, ctx.actor_id), db.get(ProviderConnection, connection_id)
    version = db.get(CollectionPolicyVersion, config.policy_version_id)
    policy = db.get(CollectionPolicy, version.policy_id) if version else None
    if (
        config.authorization_ref not in APPROVED_LIVE_AUTHORIZATIONS
        or member is None
        or actor is None
        or config.expires_at <= now()
        or config.expires_at > now() + timedelta(minutes=30)
        or config.price_source_at > now()
        or now() - config.price_source_at > timedelta(days=30)
        or connection is None
        or connection.tenant_id != tenant.id
        or not connection.encrypted_credentials_ref
        or version is None
        or policy is None
        or version.tenant_id != tenant.id
    ):
        raise HTTPException(403, "VOICE_OPERATOR_APPROVAL_REQUIRED")
    owner = db.get(User, ctx.actor_id)
    if owner is None:
        raise HTTPException(403, "HUMAN_OPERATOR_REQUIRED")
    participants = list(
        db.scalars(
            select(User).where(
                User.issuer == owner.issuer,
                func.lower(User.email) == config.participant_email,
                User.email_verified.is_(True),
            )
        )
    )
    if len(participants) != 1:
        raise HTTPException(403, "VOICE_CONSENTED_COMPANY_PARTICIPANT_REQUIRED")
    participant = participants[0]
    participant_actor = db.get(Actor, participant.id, populate_existing=True)
    participant_member = db.scalar(
        select(Membership).where(
            Membership.tenant_id == tenant.id,
            Membership.user_id == participant.id,
            Membership.active.is_(True),
        )
    )
    if (
        participant_actor is None
        or not participant_actor.active
        or participant_member is None
        or Permission.INVESTIGATE not in ROLE_PERMISSIONS[Role(participant_member.role)]
    ):
        raise HTTPException(403, "VOICE_CONSENTED_COMPANY_PARTICIPANT_REQUIRED")
    sources = []
    for key in config.invoice_ids:
        invoice = db.get(InvoiceSnapshot, key, populate_existing=True)
        if invoice is None or invoice.tenant_id != tenant.id:
            raise HTTPException(403, "VOICE_LIVE_SOURCE_SCOPE_DENIED")
        account = db.get(ProviderConnection, invoice.provider_connection_id, populate_existing=True)
        if account is None or account.tenant_id != tenant.id or account.status != "READY":
            raise HTTPException(403, "VOICE_LIVE_SOURCE_SCOPE_DENIED")
        sources.append(
            {
                "invoice_id": str(key),
                "source_revision": invoice.source_revision,
                "accounting_connection_id": str(invoice.provider_connection_id),
                "accounting_connection_version": str(account.version),
                "customer_id": str(invoice.customer_id),
            }
        )
    if len({(s["accounting_connection_id"], s["customer_id"]) for s in sources}) != 1:
        raise HTTPException(403, "VOICE_LIVE_SOURCE_SCOPE_DENIED")
    scopes = []
    caps: dict[str, str | int] = {name: 0 for name in Units.model_fields}
    caps.update(requests=config.max_requests, duration_seconds=str(config.max_total_seconds))
    for dimension in ("tenant", "provider", "run", "call", "demo_session"):
        query = select(BudgetScope).where(
            BudgetScope.tenant_id == tenant.id,
            BudgetScope.dimension == dimension,
            BudgetScope.synthetic.is_(False),
            BudgetScope.provider_connection_id.is_(None)
            if dimension == "tenant"
            else BudgetScope.provider_connection_id == connection_id,
        )
        scope = (
            live_provider_scope(db, tenant.id, connection)
            if dimension == "provider"
            else db.scalar(query)
            if dimension == "tenant"
            else None
        )
        if scope is None:
            scope = BudgetScope(
                tenant_id=tenant.id,
                provider_connection_id=None if dimension == "tenant" else connection_id,
                dimension=dimension,
                synthetic=False,
                creator_id=ctx.actor_id,
            )
            db.add(scope)
            db.flush()
        limit = db.scalar(
            select(BudgetLimit).where(
                BudgetLimit.scope_id == scope.id,
                BudgetLimit.currency == "USD",
                BudgetLimit.state == "BOUNDED_LIVE_TEST",
            )
        )
        if limit is None:
            db.add(
                BudgetLimit(
                    tenant_id=tenant.id,
                    scope_id=scope.id,
                    currency="USD",
                    state="BOUNDED_LIVE_TEST",
                    amount=config.max_usd,
                    requests=config.max_requests,
                    unit_caps=caps,
                )
            )
        elif dimension != "tenant":
            # Existing shared tenant ceiling changes only through explicit operator review.
            # Provider/grant limits may tighten, never reset prior/UNKNOWN exposure.
            limit.amount, limit.requests = (
                min(limit.amount, config.max_usd),
                min(limit.requests, config.max_requests),
            )
            limit.unit_caps = {
                name: str(min(Decimal(str(limit.unit_caps.get(name, 0))), Decimal(str(value))))
                for name, value in caps.items()
            }
        scopes.append(str(scope.id))
    grant = VoiceLiveGrant(
        tenant_id=tenant.id,
        tenant_generation=tenant.version,
        initiator_id=ctx.actor_id,
        participant_id=participant.id,
        participant_membership_version=participant_member.version,
        participant_actor_version=participant_actor.version,
        membership_version=member.version,
        actor_version=actor.version,
        connection_id=connection_id,
        connection_version=connection.version,
        credential_revision=connection.encrypted_credentials_ref,
        policy_version_id=version.id,
        policy_generation=policy.version,
        authorization_ref=config.authorization_ref,
        material=config.model_dump(mode="json"),
        source_scope=sources,
        scope_ids=scopes,
    )
    db.add(grant)
    db.flush()
    current_grant(db, grant.id, tenant.id, connection_id, require_ready=False)
    db.add(
        AuditEvent(
            tenant_id=tenant.id,
            actor_id=ctx.actor_id,
            correlation_id=ctx.correlation_id,
            action="voice.operator_live_grant_installed",
            target_id=grant.id,
            details={
                "authorization_ref": config.authorization_ref,
                "source_commit": config.source_commit,
                "capture": "DISABLED",
            },
        )
    )
    return grant


class VoiceLiveAuthorization:
    def __init__(
        self,
        grant_id: UUID,
        *,
        authorization_ref: str,
        source_commit: str,
        kill_switch: Callable[[], bool],
        integrity_check: Callable[[], bool] = lambda: True,
    ):
        self.integrity_check = integrity_check
        self.grant_id, self.authorization_ref, self.source_commit, self.kill_switch = (
            grant_id,
            authorization_ref,
            source_commit,
            kill_switch,
        )

    def validate_integrity(self) -> None:
        if not self.integrity_check():
            raise HTTPException(403, "VOICE_LIVE_CONFIGURATION_CHANGED")

    def validate(
        self,
        db: Session,
        tenant_id: UUID,
        connection_id: UUID,
        *,
        require_ready: bool = True,
        safety_stop: bool = False,
    ) -> VoiceLiveGrant:
        if not self.integrity_check():
            raise HTTPException(403, "VOICE_LIVE_CONFIGURATION_CHANGED")
        if not safety_stop and self.kill_switch():
            raise HTTPException(403, "VOICE_LIVE_KILL_SWITCH")
        grant = current_grant(db, self.grant_id, tenant_id, connection_id, require_ready=require_ready)
        config = config_for(grant)
        if (config.authorization_ref, config.source_commit) != (self.authorization_ref, self.source_commit):
            raise HTTPException(403, "VOICE_LIVE_CONFIGURATION_CHANGED")
        return grant

    def reserve(
        self,
        db: Session,
        ctx: UsageContext,
        operation: str,
        *,
        attempt_id: UUID,
        session_id: UUID | None = None,
    ) -> BudgetReservation:
        if operation not in OPERATIONS:
            raise HTTPException(403, "VOICE_LIVE_OPERATION_DENIED")
        grant = db.get(VoiceLiveGrant, self.grant_id, with_for_update=True, populate_existing=True)
        if grant is None:
            raise HTTPException(403, "VOICE_LIVE_GRANT_REQUIRED")
        self.validate(
            db,
            ctx.tenant_id,
            grant.connection_id,
            require_ready=not operation.startswith("probe_"),
            safety_stop=operation == "end_call",
        )
        attempt = db.get(UsageAttempt, attempt_id, populate_existing=True)
        if (
            attempt is None
            or (attempt.tenant_id, attempt.provider_connection_id, attempt.actor_id, attempt.correlation_id)
            != (ctx.tenant_id, grant.connection_id, ctx.actor_id, ctx.correlation_id)
            or attempt.operation != "voice_" + operation
            or attempt.outcome not in {"PENDING", "ATTEMPTED"}
            or attempt.event_id is not None
        ):
            raise HTTPException(403, "VOICE_LIVE_ATTEMPT_REQUIRED")
        if db.scalar(select(VoiceLiveRequest.id).where(VoiceLiveRequest.attempt_id == attempt_id)):
            raise HTTPException(403, "VOICE_LIVE_REQUEST_ALREADY_RESERVED")
        if operation == "end_call" and db.scalar(
            select(VoiceLiveRequest.id).where(
                VoiceLiveRequest.grant_id == grant.id,
                VoiceLiveRequest.session_id == session_id,
                VoiceLiveRequest.operation == "end_call",
            )
        ):
            raise HTTPException(409, "VOICE_LIVE_STOP_ALREADY_ATTEMPTED")
        config = config_for(grant)
        if session_id is None:
            if not operation.startswith("probe_") or ctx.actor_id != grant.initiator_id:
                raise HTTPException(403, "VOICE_LIVE_SESSION_REQUIRED")
        else:
            from aco.features.voice.models import VoiceSession

            session = db.get(VoiceSession, session_id, populate_existing=True)
            if (
                session is None
                or session.tenant_id != grant.tenant_id
                or session.live_grant_id != grant.id
                or session.actor_id != ctx.actor_id
                or session.correlation_id != ctx.correlation_id
                or session.initiator_id != grant.initiator_id
            ):
                raise HTTPException(403, "VOICE_LIVE_SESSION_REQUIRED")
        if operation == "start_browser":
            count = (
                db.scalar(
                    select(func.count())
                    .select_from(VoiceLiveRequest)
                    .where(
                        VoiceLiveRequest.grant_id == grant.id, VoiceLiveRequest.operation == "start_browser"
                    )
                )
                or 0
            )
            if count >= config.max_calls:
                raise HTTPException(403, "VOICE_LIVE_CALL_CAP")
        seconds = config.max_call_seconds if operation == "start_browser" else 0
        amount = config.api_request_usd_ceiling + seconds * config.all_component_usd_per_second
        units = Units(requests=1, duration_seconds=Decimal(seconds))
        reservation = BudgetService(db)._reserve_checked(
            ctx,
            UsageReservationRequest(
                operation_key=f"voice-live:{grant.id}:{attempt_id}",
                provider_connection_id=grant.connection_id,
                scope_ids=tuple(UUID(v) for v in grant.scope_ids),
                currency="USD",
                amount=amount,
                requests=1,
                units=units,
            ),
            allowed=True,
            limit_state="BOUNDED_LIVE_TEST",
        )
        if reservation.status == "BLOCKED":
            raise HTTPException(403, "VOICE_LIVE_BUDGET_EXHAUSTED")
        reservation.status = "UNKNOWN_SIDE_EFFECT"
        db.add(
            ReservationOutcome(
                reservation_id=reservation.id,
                outcome_key="wire_exposure",
                payload={
                    "key": "wire_exposure",
                    "status": "UNKNOWN_SIDE_EFFECT",
                    "verified_unused": False,
                    "actual_amount": None,
                    "units": units.model_dump(mode="json"),
                },
            )
        )
        db.add(
            VoiceLiveRequest(
                tenant_id=grant.tenant_id,
                grant_id=grant.id,
                session_id=session_id,
                attempt_id=attempt_id,
                reservation_id=reservation.id,
                operation=operation,
            )
        )
        db.flush()
        return reservation
