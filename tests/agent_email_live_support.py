"""LIVE-code composition with an HTTPX mock only; never a real grant or wire."""

from dataclasses import dataclass, field, replace
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from aco.core.context import TenantContext
from aco.features.communications.activation import latest
from aco.features.communications.email_live import (
    ApprovedEmailRetention,
    EmailLiveAuthorization,
    EmailLiveConfig,
    install_email_live_grant,
)
from aco.features.communications.provider_outbound import ProviderOutboundCapability
from aco.features.identity.models import ProviderConnection, now
from aco.integrations.resend.outbound import compose_outbound
from aco.integrations.resend.settings import ResendBinding


@dataclass
class OfflineLiveEmail:
    grant_id: UUID
    killed: bool = False
    unknown: bool = False
    requests: list[httpx.Request] = field(default_factory=list)


def configure_live_code(
    client: TestClient, db: Session, ctx: TenantContext, destination: str, *, max_requests: int = 3
) -> OfflineLiveEmail:
    app = client.app
    assert isinstance(app, FastAPI)
    previous = latest(db, ctx.tenant_id)
    assert previous is not None
    previous_id, storage_id = previous.id, previous.storage_connection_id
    email = ProviderConnection(
        tenant_id=ctx.tenant_id,
        provider="resend",
        provider_type="email",
        status="READY",
        encrypted_credentials_ref="offline-agent-email",
        external_account_id="offline-agent-account",
    )
    db.add(email)
    db.flush()
    grant = install_email_live_grant(
        db,
        tenant_id=ctx.tenant_id,
        initiator_id=ctx.actor_id,
        connection_id=email.id,
        storage_connection_id=storage_id,
        account_id="offline-agent-account",
        credential_revision="offline-agent-email",
        config=EmailLiveConfig(
            authorization_ref="OFFLINE-AGENT-EMAIL-FIXTURE-NOT-LIVE-AUTHORIZATION",
            source_commit="a" * 40,
            mode="OUTBOUND_TEST_ONLY",
            sender="onboarding@resend.dev",
            recipient=destination,
            reply_mailbox=None,
            provider_test_account_email=destination,
            max_received_messages=0,
            participant_approval_ref="offline-participant",
            sender_verification_ref="offline-domain",
            expires_at=now() + timedelta(minutes=20),
            max_requests=max_requests,
            max_messages=1,
            max_usd=Decimal(".03"),
            request_usd_ceiling=Decimal(".01"),
            price_source_ref="offline-ceiling-not-provider-price",
            price_source_at=now(),
        ),
        retention=ApprovedEmailRetention(
            approval_ref="offline-retention",
            jurisdiction="offline-only",
            subject="synthetic fixture participant",
            purpose="offline agent dispatch proof",
            reviewer_id=ctx.actor_id,
            approved_at=now(),
            effective_until=now() + timedelta(hours=1),
            retention_days=1,
            max_bytes=1048576,
        ),
    )
    db.commit()
    probe = OfflineLiveEmail(grant.id)
    capability = ProviderOutboundCapability(
        tenant_id=ctx.tenant_id,
        connection_id=email.id,
        credential_revision="offline-agent-email",
        account_id="offline-agent-account",
        sender="onboarding@resend.dev",
        destinations=(destination,),
        request_cap=1,
        ingress_activation_id=None,
        evidence_mode="LIVE",
        live_grant_id=grant.id,
        mode="OUTBOUND_TEST_ONLY",
        observe_sent_identity=True,
    )
    native_id = uuid4()

    def transport(request: httpx.Request) -> httpx.Response:
        probe.requests.append(request)
        if request.method == "GET":
            assert str(request.url) == f"https://api.resend.com/emails/{native_id}"
            return httpx.Response(
                200,
                json={
                    "id": str(native_id),
                    "from": capability.sender,
                    "to": [destination],
                    "created_at": now().isoformat(),
                    "message_id": "<offline-agent@example.test>",
                },
                headers={"x-request-id": "offline-agent-read"},
            )
        assert request.method == "POST" and str(request.url) == "https://api.resend.com/emails"
        assert request.headers["idempotency-key"].startswith(f"aco:{ctx.tenant_id}:{email.id}:")
        if probe.unknown:
            raise httpx.ReadTimeout("offline unknown acceptance", request=request)
        return httpx.Response(
            200, json={"id": str(native_id)}, headers={"x-request-id": "offline-agent-send"}
        )

    def binding(cap: ProviderOutboundCapability) -> ResendBinding:
        return ResendBinding(
            tenant_id=cap.tenant_id,
            connection_id=cap.connection_id,
            api_key=SecretStr("offline-placeholder"),
            inbox_addresses=(),
            sender_addresses=(cap.sender,),
            download_hosts=frozenset(),
            evidence_mode="LIVE",
        )

    runtime = compose_outbound(
        app.state.business_sessions,
        app.state.settings,
        None,
        lambda tenant, connection: capability,
        binding,
        transport_factory=lambda: httpx.MockTransport(transport),
        enabled=True,
        live_authorization=EmailLiveAuthorization(
            grant.id,
            authorization_ref=grant.authorization_ref,
            source_commit="a" * 40,
            kill_switch=lambda: probe.killed,
        ),
    )
    app.state.ports = replace(app.state.ports, email=runtime)
    base = f"/api/workspaces/{ctx.tenant_id}/email-execution/provider"
    shown = client.get(base)
    assert shown.status_code == 200, shown.text
    reviewed = client.put(
        base,
        json={
            "connection_id": str(email.id),
            "expected_activation_id": str(previous_id),
            "reviewed_hash": shown.json()["configurations"][0]["reviewed_hash"],
            "evidence": "Explicit offline MockTransport LIVE-code capability; no real provider authority",
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    db.rollback()
    return probe
