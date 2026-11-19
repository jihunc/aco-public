"""Owner-reviewed, expiring synthetic execution scope; settings never grant tenant rights."""

from datetime import timedelta
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.core.settings import Settings
from aco.features.communications.dto import EmailActivationView, EmailExecutionView, ReviewEmailExecution
from aco.features.communications.models import EmailActivationRevocation, EmailExecutionActivation
from aco.features.identity.models import Membership, ProviderConnection, Tenant, now
from aco.features.identity.service import IdentityService, Permission


def system_enabled(settings: Settings, runtime: object) -> bool:
    from aco.features.communications.provider_outbound import ProviderOutboundRuntime
    from aco.integrations.demo_email.adapter import DemoEmailRuntime

    if isinstance(runtime, ProviderOutboundRuntime):
        return runtime.available() and settings.outbound_enabled and not settings.kill_switch
    return (
        isinstance(runtime, DemoEmailRuntime)
        and settings.synthetic_email_enabled
        and settings.outbound_enabled
        and not settings.kill_switch
        and settings.external_providers == "fake"
        and settings.app_env in {"local", "demo"}
    )


def latest(db: Session, tenant: UUID) -> EmailExecutionActivation | None:
    return db.scalar(
        select(EmailExecutionActivation)
        .where(EmailExecutionActivation.tenant_id == tenant)
        .order_by(EmailExecutionActivation.created_at.desc(), EmailExecutionActivation.id.desc())
        .limit(1)
    )


def validate_activation(db: Session, tenant: Tenant, row: EmailExecutionActivation) -> None:
    member = db.scalar(
        select(Membership)
        .where(Membership.tenant_id == tenant.id, Membership.user_id == row.reviewer_id)
        .execution_options(populate_existing=True)
    )
    revoked = db.scalar(
        select(EmailActivationRevocation.id).where(
            EmailActivationRevocation.tenant_id == tenant.id,
            EmailActivationRevocation.activation_id == row.id,
        )
    )
    if (
        not tenant.is_demo
        or row.tenant_id != tenant.id
        or revoked
        or row.expires_at <= now()
        or member is None
        or not member.active
        or member.role != "owner"
        or member.version != row.membership_version
    ):
        raise HTTPException(403, "EMAIL_EXECUTION_REVIEW_REQUIRED")
    for key, version, kind, provider in (
        (
            row.email_connection_id,
            row.email_connection_version,
            "email",
            "demo_email" if row.provider_binding is None else "resend",
        ),
        (row.storage_connection_id, row.storage_connection_version, "storage", None),
    ):
        connection = db.get(ProviderConnection, key, populate_existing=True)
        if (
            connection is None
            or connection.tenant_id != tenant.id
            or connection.version != version
            or connection.provider_type != kind
            or connection.status != "READY"
            or (provider and connection.provider != provider)
        ):
            raise HTTPException(403, "EMAIL_CONNECTION_CHANGED")

    if row.provider_binding is not None:
        from aco.features.communications.provider_outbound import persisted_outbound

        persisted_outbound(db, row)


class EmailActivationService:
    def __init__(self, db: Session, settings: Settings, runtime: object):
        self.db, self.settings, self.runtime = db, settings, runtime

    def view(self, ctx: TenantContext) -> EmailExecutionView:
        tenant = IdentityService(self.db).authorize(ctx, Permission.INVOICE_READ)
        row = latest(self.db, tenant.id)
        active = False
        if row:
            try:
                validate_activation(self.db, tenant, row)
                if row.provider_binding is None:
                    from aco.integrations.demo_email.adapter import DemoEmailRuntime

                    if not isinstance(self.runtime, DemoEmailRuntime):
                        raise HTTPException(403, "EMAIL_EXECUTOR_CHANGED")
                else:
                    from aco.features.communications.provider_outbound import ProviderOutboundRuntime

                    if not isinstance(self.runtime, ProviderOutboundRuntime):
                        raise HTTPException(403, "PROVIDER_OUTBOUND_DISABLED")
                    self.runtime.validate(self.db, row)
                active = True
            except HTTPException:
                pass
        from aco.features.communications.provider_outbound import ProviderOutboundRuntime
        from aco.integrations.demo_email.adapter import DemoEmailRuntime

        mode: Literal["DISABLED", "SYNTHETIC", "MOCKED_HTTP", "LIVE"] = "DISABLED"
        if row and row.provider_binding is not None:
            mode = "LIVE" if row.provider_binding.get("evidence_mode") == "LIVE" else "MOCKED_HTTP"
        elif row:
            mode = "SYNTHETIC"
        return EmailExecutionView(
            system_enabled=system_enabled(self.settings, self.runtime),
            synthetic=mode != "LIVE",
            provider="resend"
            if isinstance(self.runtime, ProviderOutboundRuntime)
            else "demo_email"
            if isinstance(self.runtime, DemoEmailRuntime)
            else None,
            mode=mode,
            activation=EmailActivationView(
                **{key: getattr(row, key) for key in EmailActivationView.model_fields if key != "current"},
                current=active,
            )
            if row
            else None,
        )

    def review(self, ctx: TenantContext, command: ReviewEmailExecution) -> EmailExecutionView:
        tenant = IdentityService(self.db).authorize(ctx, Permission.SETTINGS)
        if not tenant.is_demo or self.settings.app_env not in {"local", "demo"}:
            raise HTTPException(403, "SYNTHETIC_EMAIL_ONLY")
        from aco.integrations.demo_email.adapter import DemoEmailRuntime

        if not isinstance(self.runtime, DemoEmailRuntime):
            raise HTTPException(503, "EMAIL_EXECUTOR_UNAVAILABLE")
        prior = latest(self.db, tenant.id)
        if (prior.id if prior else None) != command.expected_activation_id:
            raise HTTPException(409, "EMAIL_ACTIVATION_CHANGED")
        storage = self.db.scalar(
            select(ProviderConnection).where(
                ProviderConnection.tenant_id == tenant.id,
                ProviderConnection.provider_type == "storage",
                ProviderConnection.status == "READY",
            )
        )
        if storage is None:
            raise HTTPException(409, "STORAGE_CONNECTION_REQUIRED")
        email = self.db.scalar(
            select(ProviderConnection).where(
                ProviderConnection.tenant_id == tenant.id,
                ProviderConnection.provider_type == "email",
                ProviderConnection.provider == "demo_email",
            )
        )
        if email is None:
            email = ProviderConnection(
                tenant_id=tenant.id,
                provider="demo_email",
                provider_type="email",
                status="READY",
                external_account_id="synthetic:" + str(tenant.id),
                capabilities=["send_email", "get_message", "synthetic"],
            )
            self.db.add(email)
            self.db.flush()
        if email.status != "READY":
            raise HTTPException(409, "EMAIL_CONNECTION_UNAVAILABLE")
        member = self.db.scalar(
            select(Membership).where(Membership.tenant_id == tenant.id, Membership.user_id == ctx.actor_id)
        )
        assert member
        if prior:
            existing = self.db.scalar(
                select(EmailActivationRevocation).where(EmailActivationRevocation.activation_id == prior.id)
            )
            if existing is None:
                self.db.add(
                    EmailActivationRevocation(
                        tenant_id=tenant.id,
                        activation_id=prior.id,
                        actor_id=ctx.actor_id,
                        reason="superseded_by_new_review",
                    )
                )
        row = EmailExecutionActivation(
            tenant_id=tenant.id,
            email_connection_id=email.id,
            email_connection_version=email.version,
            storage_connection_id=storage.id,
            storage_connection_version=storage.version,
            reviewer_id=ctx.actor_id,
            membership_version=member.version,
            sender=command.sender,
            destinations=command.destinations,
            request_cap=command.request_cap,
            capture_policy="synthetic_30_days",
            evidence=command.evidence,
            expires_at=now() + timedelta(days=1),
        )
        self.db.add(row)
        self.db.flush()
        IdentityService(self.db).audit(ctx, "email.synthetic_execution_reviewed", row.id)
        return self.view(ctx)
