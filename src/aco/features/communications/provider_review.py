"""Owner-only company review of trusted server bindings. No SDK or credential resolution."""

import hashlib
import json
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.core.settings import Settings
from aco.features.communications.activation import latest
from aco.features.communications.dto import (
    EmailExecutionView,
    ProviderEmailConfiguration,
    ProviderEmailReviewView,
    ReviewProviderEmailExecution,
)
from aco.features.communications.provider_outbound import (
    ProviderOutboundRuntime,
    ReviewProviderOutbound,
    validate_live_material,
)
from aco.features.identity.models import ProviderConnection
from aco.features.identity.service import IdentityService, Permission


class ProviderEmailReviewService:
    def __init__(self, db: Session, settings: Settings, runtime: object):
        self.db, self.settings, self.runtime = db, settings, runtime

    def configuration(self, ctx: TenantContext, connection_id: UUID) -> ProviderEmailConfiguration:
        from aco.features.communications.email_live import config_for
        from aco.features.communications.ingress import current_activation

        IdentityService(self.db).authorize(ctx, Permission.SETTINGS)
        if not isinstance(self.runtime, ProviderOutboundRuntime):
            raise HTTPException(503, "PROVIDER_OUTBOUND_UNAVAILABLE")
        connection = self.db.get(ProviderConnection, connection_id, populate_existing=True)
        if (
            connection is None
            or connection.tenant_id != ctx.tenant_id
            or connection.provider_type != "email"
            or connection.provider != "resend"
            or connection.status != "READY"
        ):
            raise HTTPException(404, "EMAIL_CONNECTION_UNAVAILABLE")
        cap = self.runtime.capability(ctx.tenant_id, connection_id)
        if (
            cap.account_id != connection.external_account_id
            or cap.credential_revision != connection.encrypted_credentials_ref
        ):
            raise HTTPException(409, "PROVIDER_OUTBOUND_CONFIGURATION_CHANGED")
        approved = None
        if cap.evidence_mode == "LIVE":
            assert self.runtime.live_authorization is not None
            grant = self.runtime.live_authorization.validate(self.db, ctx.tenant_id, connection_id)
            validate_live_material(self.db, cap)
            approved = config_for(grant)
        if cap.mode == "ROUNDTRIP_TEST":
            if self.runtime.ingress_runtime is None or cap.ingress_activation_id is None:
                raise HTTPException(403, "PROVIDER_OUTBOUND_BINDING_INVALID")
            inbound = current_activation(self.db, cap.ingress_activation_id, ctx.tenant_id)
            captured = self.runtime.ingress_runtime.validate(self.db, inbound)
            if captured.connection_id != connection_id or cap.sender not in captured.inbox_addresses:
                raise HTTPException(403, "PROVIDER_OUTBOUND_BINDING_INVALID")
            expires = inbound.expires_at
        else:
            assert approved is not None
            expires = approved.expires_at
        prior = latest(self.db, ctx.tenant_id)
        expected = prior.id if prior else None
        digest = hashlib.sha256(
            json.dumps(
                {
                    "capability": cap.model_dump(mode="json"),
                    "expected_activation_id": str(expected) if expected else None,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return ProviderEmailConfiguration(
            connection_id=connection_id,
            expected_activation_id=expected,
            reviewed_hash=digest,
            evidence_mode=cap.evidence_mode,
            mode=cap.mode,
            sender=cap.sender,
            destinations=list(cap.destinations),
            request_cap=cap.request_cap,
            expires_at=expires,
            max_requests=approved.max_requests if approved else None,
            max_usd=str(approved.max_usd) if approved else None,
        )

    def view(self, ctx: TenantContext) -> ProviderEmailReviewView:
        IdentityService(self.db).authorize(ctx, Permission.SETTINGS)
        if not isinstance(self.runtime, ProviderOutboundRuntime) or not self.runtime.available():
            return ProviderEmailReviewView(
                available=False, configurations=[], reason="PROVIDER_OUTBOUND_UNAVAILABLE"
            )
        configurations = []
        for key in self.db.scalars(
            select(ProviderConnection.id)
            .where(
                ProviderConnection.tenant_id == ctx.tenant_id,
                ProviderConnection.provider_type == "email",
                ProviderConnection.provider == "resend",
                ProviderConnection.status == "READY",
            )
            .order_by(ProviderConnection.id)
        ):
            try:
                configurations.append(self.configuration(ctx, key))
            except HTTPException:
                # Do not disclose unrelated bindings, private credentials or another company.
                continue
        return ProviderEmailReviewView(
            available=bool(configurations),
            configurations=configurations,
            reason=None if configurations else "PROVIDER_REVIEW_UNAVAILABLE",
        )

    def review(self, ctx: TenantContext, command: ReviewProviderEmailExecution) -> EmailExecutionView:
        # Serialize captured review and replacement under the same tenant lock as activate().
        from aco.features.identity.models import Tenant

        IdentityService(self.db).authorize(ctx, Permission.SETTINGS)
        self.db.get(Tenant, ctx.tenant_id, populate_existing=True, with_for_update={"key_share": True})
        current = self.configuration(ctx, command.connection_id)
        if (
            current.reviewed_hash != command.reviewed_hash
            or current.expected_activation_id != command.expected_activation_id
        ):
            raise HTTPException(409, "PROVIDER_EMAIL_REVIEW_CHANGED")
        assert isinstance(self.runtime, ProviderOutboundRuntime)
        return self.runtime.activate(
            self.db,
            ctx,
            ReviewProviderOutbound(
                connection_id=command.connection_id,
                expected_activation_id=command.expected_activation_id,
                evidence=command.evidence,
            ),
        )
