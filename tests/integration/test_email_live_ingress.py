"""Opt-in LIVE-label authority with offline HTTP only; this does not authorize live testing."""

import base64
import json
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any
from uuid import uuid4

import httpx
from action_support import action_world
from pydantic import SecretStr
from sqlalchemy import func, select
from svix.webhooks import Webhook

from aco.features.communications.email_live import (
    ApprovedEmailRetention,
    EmailLiveAuthorization,
    EmailLiveConfig,
    install_email_live_grant,
)
from aco.features.communications.ingress import EmailIngressService
from aco.features.communications.models import EmailIngressReceipt
from aco.features.communications.provider_ingress import (
    Authority,
    ProviderIngressCapability,
    ProviderIngressRuntime,
)
from aco.features.identity.models import ProviderConnection, now
from aco.integrations.ports.email import EmailPort
from aco.integrations.ports.email_observer import EmailRequestObserver
from aco.integrations.resend.routing import ResendWebhookRouter
from aco.integrations.resend.runtime import ResendRuntime
from aco.integrations.resend.settings import ResendBinding


def test_explicit_live_ingress_requires_current_grant_signed_participant_and_distinct_message_cap() -> None:
    with action_world() as world:
        db, ctx, client = world.store, world.ctx, world.client
        email = ProviderConnection(
            tenant_id=ctx.tenant_id,
            provider="resend",
            provider_type="email",
            status="READY",
            external_account_id="offline-shared-account",
            encrypted_credentials_ref="offline-revision",
        )
        storage = ProviderConnection(
            tenant_id=ctx.tenant_id,
            provider="s3",
            provider_type="storage",
            status="READY",
        )
        db.add_all([email, storage])
        db.commit()
        at = now()
        config = EmailLiveConfig(
            authorization_ref="OFFLINE-TEST-NOT-A-LIVE-AUTHORIZATION",
            source_commit="a" * 40,
            sender="company-a@aco.example.com",
            recipient="participant@example.test",
            reply_mailbox="company-a@aco.example.com",
            participant_approval_ref="offline-fixture",
            sender_verification_ref="offline-fixture",
            expires_at=at + timedelta(minutes=10),
            max_requests=3,
            max_messages=1,
            max_received_messages=1,
            max_usd=Decimal(".03"),
            request_usd_ceiling=Decimal(".01"),
            price_source_ref="offline-fixture",
            price_source_at=at,
        )
        grant = install_email_live_grant(
            db,
            tenant_id=ctx.tenant_id,
            initiator_id=ctx.actor_id,
            connection_id=email.id,
            storage_connection_id=storage.id,
            account_id="offline-shared-account",
            credential_revision="offline-revision",
            config=config,
            retention=ApprovedEmailRetention(
                approval_ref="offline-approved-test-retention",
                jurisdiction="offline-fixture",
                subject="explicit synthetic test participant",
                purpose="offline guarded capture proof",
                reviewer_id=ctx.actor_id,
                approved_at=at,
                effective_until=at + timedelta(hours=1),
                retention_days=1,
                max_bytes=1048576,
            ),
        )
        db.commit()
        secret = "whsec_" + base64.b64encode(b"offline-test-account-signature-key").decode()
        assert config.reply_mailbox is not None
        cap = ProviderIngressCapability(
            tenant_id=ctx.tenant_id,
            connection_id=email.id,
            credential_revision="offline-revision",
            inbox_addresses=(config.reply_mailbox,),
            webhook_secret_sha256=sha256(secret.encode()).hexdigest(),
            retention_policy_id=grant.retention_policy_id,
            retention_policy_version=grant.retention_policy_version,
            capture_purpose="approved_live_test_capture",
            evidence_mode="LIVE",
            live_grant_id=grant.id,
        )
        killed = False
        guard = EmailLiveAuthorization(
            grant.id,
            authorization_ref=config.authorization_ref,
            source_commit=config.source_commit,
            kill_switch=lambda: killed,
        )
        wires: list[str] = []

        def transport(request: httpx.Request) -> httpx.Response:
            wires.append(str(request.url))
            raise AssertionError("Webhook admission must never fetch or send")

        def port(
            capability: ProviderIngressCapability, authority: Authority, observer: EmailRequestObserver
        ) -> EmailPort:
            binding = ResendBinding(
                tenant_id=capability.tenant_id,
                connection_id=capability.connection_id,
                api_key=SecretStr("offline-placeholder"),
                inbox_addresses=capability.inbox_addresses,
                download_hosts=frozenset({"storage.example.test"}),
                webhook_secret=SecretStr(secret),
                evidence_mode="LIVE",
            )
            return ResendRuntime(
                lambda tenant, connection: binding,
                authority=authority,
                enabled=True,
                observer=observer,
                transport_factory=lambda: httpx.MockTransport(transport),
            ).bind(capability.tenant_id, capability.connection_id)

        runtime = ProviderIngressRuntime(
            client.app.state.business_sessions,
            lambda tenant, connection: cap,
            port,
            enabled=True,
            live_authorization=guard,
        )
        endpoint = uuid4()
        service = EmailIngressService(
            client.app.state.business_sessions,
            provider_ingress=runtime,
            shared_webhooks={
                endpoint: ResendWebhookRouter(
                    endpoint_id=endpoint,
                    account_id="offline-shared-account",
                    secret=SecretStr(secret),
                )
            },
        )
        client.app.state.email_ingress = service
        connection_id, storage_id = email.id, storage.id
        db.rollback()
        response = client.post(
            f"/api/workspaces/{ctx.tenant_id}/email-ingress/provider-activations",
            json={
                "email_connection_id": str(connection_id),
                "storage_connection_id": str(storage_id),
                "evidence": "Offline explicit approved-participant capture fixture only",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["synthetic"] is False
        path = f"/api/email-webhooks/{endpoint}"

        def notify(sender: str, native: str, event: str) -> Any:
            raw = json.dumps(
                {
                    "type": "email.received",
                    "created_at": at.isoformat(),
                    "data": {
                        "email_id": native,
                        "to": [config.reply_mailbox],
                        "from": sender,
                    },
                }
            )
            result = client.post(
                path,
                content=raw,
                headers={
                    "svix-id": event,
                    "svix-timestamp": str(int(at.timestamp())),
                    "svix-signature": Webhook(secret).sign(event, at, raw),
                },
            )
            return result

        native = str(uuid4())
        first = notify(config.recipient, native, "live_fixture_1")
        assert first.status_code == 202, first.text
        assert notify(config.recipient, native, "live_fixture_1").json()[0]["id"] == first.json()[0]["id"]
        assert notify("unapproved@example.test", str(uuid4()), "live_unapproved").status_code == 403
        assert notify(config.recipient, str(uuid4()), "live_over_cap").status_code == 403
        killed = True
        assert notify(config.recipient, native, "live_fixture_1").status_code == 403
        assert db.scalar(select(func.count()).select_from(EmailIngressReceipt)) == 1
        assert wires == []
