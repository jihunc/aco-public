"""Operator-installed bounded email test authority, distinct from credentials and app Owner review.

No HTTP installer, provider calls, or environment-file loading. A grant never authorizes an
outbound action itself: current worker/contact/policy/source and capture guards still apply.
"""

import json
import os
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import AwareDatetime, Field, field_validator, model_validator
from sqlalchemy import JSON, DateTime, ForeignKey, ForeignKeyConstraint, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from aco.core.live import DELIVERY_LIVE_AUTHORIZATION
from aco.core.types import StrictModel
from aco.features.artifacts.models import RetentionPolicy
from aco.features.identity.models import (
    Actor,
    AuditEvent,
    Base,
    Membership,
    ProviderConnection,
    Record,
    Tenant,
    now,
)
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


class ApprovedEmailRetention(StrictModel):
    approval_ref: str = Field(min_length=1, max_length=200)
    jurisdiction: str = Field(min_length=1, max_length=200)
    subject: str = Field(min_length=1, max_length=500)
    purpose: str = Field(min_length=1, max_length=500)
    reviewer_id: UUID
    approved_at: AwareDatetime
    effective_until: AwareDatetime
    retention_days: int = Field(ge=1, le=30)
    max_bytes: int = Field(ge=1, le=10485760)

    @field_validator("approval_ref", "jurisdiction", "subject", "purpose")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if value != value.strip() or not value:
            raise ValueError("Explicit approved retention material required")
        return value


class EmailLiveConfig(StrictModel):
    authorization_ref: str = Field(min_length=1, max_length=200)
    source_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    mode: Literal["ROUNDTRIP_TEST", "OUTBOUND_TEST_ONLY"] = "ROUNDTRIP_TEST"
    sender: str
    recipient: str
    reply_mailbox: str | None
    provider_test_account_email: str | None = None
    participant_approval_ref: str = Field(min_length=1, max_length=200)
    sender_verification_ref: str = Field(min_length=1, max_length=200)
    expires_at: AwareDatetime
    max_requests: int = Field(ge=1, le=20)
    max_messages: int = Field(ge=1, le=3)
    max_received_messages: int = Field(default=1, ge=0, le=3)
    max_usd: Decimal = Field(gt=0, le=Decimal("1"), decimal_places=12)
    request_usd_ceiling: Decimal = Field(gt=0, le=Decimal("1"), decimal_places=12)
    price_source_ref: str = Field(min_length=1, max_length=500)
    price_source_at: AwareDatetime

    @field_validator("sender", "recipient")
    @classmethod
    def normalized_mailbox(cls, value: str) -> str:
        return mailbox(value).casefold()

    @field_validator("reply_mailbox", "provider_test_account_email")
    @classmethod
    def optional_mailbox(cls, value: str | None) -> str | None:
        return mailbox(value).casefold() if value is not None else None

    @field_validator(
        "authorization_ref", "participant_approval_ref", "sender_verification_ref", "price_source_ref"
    )
    @classmethod
    def explicit_ref(cls, value: str) -> str:
        if value != value.strip() or not value:
            raise ValueError("Explicit approval/evidence reference required")
        return value

    @model_validator(mode="after")
    def bounded(self) -> "EmailLiveConfig":
        if self.request_usd_ceiling > self.max_usd or self.max_messages > self.max_requests:
            raise ValueError("Per-request and message bounds must fit the aggregate grant")
        if self.recipient == self.sender:
            raise ValueError("A distinct approved participant is required")
        if self.authorization_ref == DELIVERY_LIVE_AUTHORIZATION:
            # Independent server-only operator approval, never grant/model/browser material.
            # Rechecked by config_for on every current-grant authorization, not just install.
            try:
                approved = mailbox(os.environ.get("ACO_DELIVERY_APPROVED_EMAIL_RECIPIENT", "")).casefold()
            except ValueError:
                raise ValueError("Explicit private operator email recipient approval required") from None
            if (
                self.mode != "OUTBOUND_TEST_ONLY"
                or self.recipient != approved
                or self.provider_test_account_email != approved
            ):
                raise ValueError("Final delivery requires the sole approved private operator recipient")
        if self.mode == "OUTBOUND_TEST_ONLY":
            if (
                self.sender != "onboarding@resend.dev"
                or self.reply_mailbox is not None
                or self.max_received_messages != 0
                or self.provider_test_account_email != self.recipient
            ):
                raise ValueError(
                    "Restricted test sender/account recipient without receiving authority required"
                )
        elif self.sender != self.reply_mailbox or self.max_received_messages < 1:
            raise ValueError("One owned reply mailbox and explicit receiving bound required")
        return self


class EmailLiveGrant(Record, Base):
    __tablename__ = "email_live_grants"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("authorization_ref"),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"], ["provider_connections.tenant_id", "provider_connections.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "storage_connection_id"],
            ["provider_connections.tenant_id", "provider_connections.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "retention_policy_id"],
            ["artifact_retention_policies.tenant_id", "artifact_retention_policies.id"],
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    tenant_generation: Mapped[int]
    initiator_id: Mapped[UUID] = mapped_column(ForeignKey("identity_actors.id"))
    membership_version: Mapped[int]
    connection_id: Mapped[UUID]
    connection_version: Mapped[int]
    storage_connection_id: Mapped[UUID]
    storage_connection_version: Mapped[int]
    account_id: Mapped[str] = mapped_column(Text)
    credential_revision: Mapped[str] = mapped_column(Text)
    authorization_ref: Mapped[str] = mapped_column(Text)
    material: Mapped[dict[str, object]] = mapped_column(JSON)
    retention_policy_id: Mapped[UUID]
    retention_policy_version: Mapped[int]
    scope_ids: Mapped[list[str]] = mapped_column(JSON)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def config_for(grant: EmailLiveGrant) -> EmailLiveConfig:
    return EmailLiveConfig.model_validate_json(json.dumps(grant.material))


def current_email_live_grant(
    db: Session, grant_id: UUID, tenant_id: UUID, connection_id: UUID
) -> EmailLiveGrant:
    grant = db.get(EmailLiveGrant, grant_id, populate_existing=True)
    if (
        grant is None
        or (grant.tenant_id, grant.connection_id) != (tenant_id, connection_id)
        or grant.revoked_at
    ):
        raise HTTPException(403, "EMAIL_LIVE_GRANT_REQUIRED")
    config = config_for(grant)
    tenant = db.get(Tenant, tenant_id, populate_existing=True)
    member = db.scalar(
        select(Membership)
        .where(Membership.tenant_id == tenant_id, Membership.user_id == grant.initiator_id)
        .execution_options(populate_existing=True)
    )
    actor = db.get(Actor, grant.initiator_id, populate_existing=True)
    email = db.get(ProviderConnection, connection_id, populate_existing=True)
    storage = db.get(ProviderConnection, grant.storage_connection_id, populate_existing=True)
    policy = db.get(RetentionPolicy, grant.retention_policy_id, populate_existing=True)
    if (
        config.expires_at <= now()
        or config.price_source_at > now()
        or config.price_source_at < now() - timedelta(days=30)
        or grant.authorization_ref != config.authorization_ref
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
        or email is None
        or email.tenant_id != tenant_id
        or email.provider != "resend"
        or email.provider_type != "email"
        or email.status != "READY"
        or email.version != grant.connection_version
        or email.external_account_id != grant.account_id
        or email.encrypted_credentials_ref != grant.credential_revision
        or storage is None
        or storage.tenant_id != tenant_id
        or storage.provider_type != "storage"
        or storage.status != "READY"
        or storage.version != grant.storage_connection_version
        or policy is None
        or policy.tenant_id != tenant_id
        or policy.version != grant.retention_policy_version
        or policy.synthetic
        or policy.approval is None
    ):
        raise HTTPException(403, "EMAIL_LIVE_GRANT_REVOKED")
    approval = ApprovedEmailRetention.model_validate_json(json.dumps(policy.approval))
    if (
        approval.effective_until <= now()
        or approval.approved_at > now()
        or approval.reviewer_id != grant.initiator_id
        or policy.authority != approval.approval_ref
        or policy.retention_days != approval.retention_days
    ):
        raise HTTPException(403, "EMAIL_LIVE_RETENTION_REVOKED")
    return grant


def install_email_live_grant(
    db: Session,
    *,
    tenant_id: UUID,
    initiator_id: UUID,
    connection_id: UUID,
    storage_connection_id: UUID,
    account_id: str,
    credential_revision: str,
    config: EmailLiveConfig,
    retention: ApprovedEmailRetention,
) -> EmailLiveGrant:
    """Trusted operator only; caller supplies actual external approval, never inferred from keys.

    Existing aggregate exposure limits are only tightened; this installer cannot reset old
    requests/spend or increase a previous tenant/provider live allowance.
    """
    config = EmailLiveConfig.model_validate_json(config.model_dump_json())
    retention = ApprovedEmailRetention.model_validate_json(retention.model_dump_json())
    tenant = db.get(Tenant, tenant_id, with_for_update={"key_share": True}, populate_existing=True)
    member = db.scalar(
        select(Membership).where(Membership.tenant_id == tenant_id, Membership.user_id == initiator_id)
    )
    email = db.get(ProviderConnection, connection_id, populate_existing=True)
    storage = db.get(ProviderConnection, storage_connection_id, populate_existing=True)
    if (
        tenant is None
        or not tenant.is_demo
        or member is None
        or not member.active
        or member.role != "owner"
        or email is None
        or email.tenant_id != tenant_id
        or email.provider != "resend"
        or email.provider_type != "email"
        or email.status != "READY"
        or not account_id
        or email.external_account_id != account_id
        or not credential_revision
        or email.encrypted_credentials_ref != credential_revision
        or storage is None
        or storage.tenant_id != tenant_id
        or storage.provider_type != "storage"
        or storage.status != "READY"
        or config.expires_at <= now()
        or config.expires_at > now() + timedelta(hours=1)
        or config.price_source_at > now()
        or config.price_source_at < now() - timedelta(days=30)
        or retention.reviewer_id != initiator_id
        or retention.approved_at > now()
        or retention.effective_until < config.expires_at
    ):
        raise HTTPException(403, "EMAIL_OPERATOR_APPROVAL_REQUIRED")
    key = uuid4()
    policy = RetentionPolicy(
        tenant_id=tenant_id,
        policy_key="approved_email_test:" + str(key),
        synthetic=False,
        retention_days=retention.retention_days,
        authority=retention.approval_ref,
        approval=retention.model_dump(mode="json"),
    )
    db.add(policy)
    db.flush()
    scopes = []
    for dimension in ("tenant", "provider", "run", "call", "demo_session"):
        scope = live_provider_scope(db, tenant_id, email) if dimension == "provider" else None
        if dimension == "tenant":
            scope = db.scalar(
                select(BudgetScope).where(
                    BudgetScope.tenant_id == tenant_id,
                    BudgetScope.dimension == "tenant",
                    BudgetScope.synthetic.is_(False),
                    BudgetScope.provider_connection_id.is_(None),
                )
            )
        if scope is None:
            scope = BudgetScope(
                tenant_id=tenant_id,
                provider_connection_id=None if dimension == "tenant" else connection_id,
                dimension=dimension,
                synthetic=False,
                creator_id=initiator_id,
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
        caps: dict[str, str | int] = {k: 0 for k in Units.model_fields}
        caps.update(requests=config.max_requests, messages=config.max_messages)
        if limit is None:
            db.add(
                BudgetLimit(
                    tenant_id=tenant_id,
                    scope_id=scope.id,
                    currency="USD",
                    amount=config.max_usd,
                    requests=config.max_requests,
                    unit_caps=caps,
                    state="BOUNDED_LIVE_TEST",
                )
            )
        elif dimension != "tenant":
            limit.amount = min(limit.amount, config.max_usd)
            limit.requests = min(limit.requests, config.max_requests)
            limit.unit_caps = {
                k: str(min(Decimal(str(limit.unit_caps.get(k, 0))), Decimal(str(v)))) for k, v in caps.items()
            }
        scopes.append(str(scope.id))
    grant = EmailLiveGrant(
        id=key,
        tenant_id=tenant_id,
        tenant_generation=tenant.version,
        initiator_id=initiator_id,
        membership_version=member.version,
        connection_id=connection_id,
        connection_version=email.version,
        storage_connection_id=storage_connection_id,
        storage_connection_version=storage.version,
        account_id=account_id,
        credential_revision=credential_revision,
        authorization_ref=config.authorization_ref,
        material=config.model_dump(mode="json"),
        retention_policy_id=policy.id,
        retention_policy_version=policy.version,
        scope_ids=scopes,
    )
    db.add(grant)
    db.flush()
    current_email_live_grant(db, grant.id, tenant_id, connection_id)
    db.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor_id=initiator_id,
            correlation_id=uuid4(),
            action="email.operator_live_grant_installed",
            target_id=grant.id,
            details={"authorization_ref": config.authorization_ref, "source_commit": config.source_commit},
        )
    )
    return grant


class EmailLiveAuthorization:
    def __init__(
        self, grant_id: UUID, *, authorization_ref: str, source_commit: str, kill_switch: Callable[[], bool]
    ):
        self.grant_id, self.authorization_ref, self.source_commit, self.kill_switch = (
            grant_id,
            authorization_ref,
            source_commit,
            kill_switch,
        )

    def validate(self, db: Session, tenant_id: UUID, connection_id: UUID) -> EmailLiveGrant:
        if self.kill_switch():
            raise HTTPException(403, "EMAIL_LIVE_KILL_SWITCH")
        grant = current_email_live_grant(db, self.grant_id, tenant_id, connection_id)
        config = config_for(grant)
        if config.authorization_ref != self.authorization_ref or config.source_commit != self.source_commit:
            raise HTTPException(403, "EMAIL_LIVE_CONFIGURATION_CHANGED")
        return grant

    def retention(self, db: Session, tenant_id: UUID, storage_connection_id: UUID) -> RetentionPolicy:
        grant = db.get(EmailLiveGrant, self.grant_id, populate_existing=True)
        if (
            grant is None
            or grant.tenant_id != tenant_id
            or grant.storage_connection_id != storage_connection_id
        ):
            raise HTTPException(403, "EMAIL_LIVE_STORAGE_SCOPE")
        self.validate(db, tenant_id, grant.connection_id)
        policy = db.get(RetentionPolicy, grant.retention_policy_id, populate_existing=True)
        assert policy is not None
        return policy

    def reserve(
        self, db: Session, ctx: UsageContext, connection_id: UUID, operation: str, *, attempt_id: UUID
    ) -> BudgetReservation:
        grant = self.validate(db, ctx.tenant_id, connection_id)
        if operation not in {
            "send_email",
            "get_message",
            "get_sent_message",
            "download_original",
            "get_attachment",
        }:
            raise HTTPException(403, "EMAIL_LIVE_OPERATION_UNAPPROVED")
        # Serialize with revocation and all concurrent requests without locking parent tenant FKs.
        db.refresh(grant, with_for_update={"key_share": True})
        self.validate(db, ctx.tenant_id, connection_id)
        attempt = db.get(UsageAttempt, attempt_id, populate_existing=True)
        if (
            attempt is None
            or (attempt.tenant_id, attempt.provider_connection_id, attempt.actor_id, attempt.correlation_id)
            != (ctx.tenant_id, connection_id, ctx.actor_id, ctx.correlation_id)
            or attempt.operation
            != (
                "send_email"
                if operation == "send_email"
                else "email_submission_get_message"
                if operation == "get_sent_message"
                else f"email_ingress_{operation}"
            )
            or attempt.outcome not in {"PENDING", "ATTEMPTED"}
            or attempt.event_id is not None
        ):
            raise HTTPException(403, "EMAIL_LIVE_ATTEMPT_REQUIRED")
        operation_key = f"email-live:{grant.id}:{attempt.id}"
        if db.scalar(
            select(BudgetReservation.id).where(
                BudgetReservation.tenant_id == ctx.tenant_id,
                BudgetReservation.provider_connection_id == connection_id,
                BudgetReservation.operation_key == operation_key,
            )
        ):
            raise HTTPException(403, "EMAIL_LIVE_REQUEST_ALREADY_RESERVED")
        config = config_for(grant)
        units = Units(requests=1, messages=1 if operation == "send_email" else 0)
        reservation = BudgetService(db)._reserve_checked(
            ctx,
            UsageReservationRequest(
                operation_key=operation_key,
                provider_connection_id=connection_id,
                scope_ids=tuple(UUID(key) for key in grant.scope_ids),
                currency="USD",
                amount=config.request_usd_ceiling,
                requests=1,
                units=units,
            ),
            allowed=True,
            limit_state="BOUNDED_LIVE_TEST",
        )
        if reservation.status == "BLOCKED":
            raise HTTPException(403, "EMAIL_LIVE_BUDGET_EXHAUSTED")
        # Billing is unknown even on HTTP success: retain the full authorized exposure.
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
        db.flush()
        return reservation
