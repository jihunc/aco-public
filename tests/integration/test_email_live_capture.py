"""LIVE-label application authority under explicit MockTransport; no live proof/permission."""

import json
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from ingress_support import ingress_settings, ingress_world
from pydantic import SecretStr
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from svix.webhooks import Webhook

from aco.features.artifacts.models import Artifact
from aco.features.artifacts.service import ArtifactService
from aco.features.communications.email_live import (
    ApprovedEmailRetention,
    EmailLiveAuthorization,
    EmailLiveConfig,
    install_email_live_grant,
)
from aco.features.communications.ingress import EmailIngressService, dispatch_email_ingress
from aco.features.communications.models import Communication, CommunicationSource, EmailIngressReceipt
from aco.features.communications.provider_ingress import (
    Authority,
    ProviderIngressCapability,
    ProviderIngressRuntime,
)
from aco.features.identity.models import ProviderConnection, now
from aco.features.usage.models import BudgetReservation, ProviderUsageEvent, UsageAttempt
from aco.integrations.ports.email import EmailPort
from aco.integrations.ports.email_observer import EmailRequestObserver
from aco.integrations.ports.storage import StorageInvocationContext
from aco.integrations.resend.routing import ResendWebhookRouter
from aco.integrations.resend.runtime import ResendRuntime
from aco.integrations.resend.settings import ResendBinding
from aco.worker.app import WorkerRuntime


@pytest.mark.parametrize("late_kill", [False, True])
async def test_approved_capture_uses_current_retention_and_stops_after_intent_kill(
    late_kill: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with ingress_world() as lane:
        world, at = lane.world, now()
        db, ctx = world.store, world.ctx
        app = cast(FastAPI, world.client.app)
        original = lane.message()
        assert original.received_at is not None and original.source.content is not None
        assert original.sender is not None
        email = ProviderConnection(
            tenant_id=ctx.tenant_id,
            provider="resend",
            provider_type="email",
            status="READY",
            external_account_id="offline-account",
            encrypted_credentials_ref="offline-revision",
        )
        db.add(email)
        db.commit()
        config = EmailLiveConfig(
            authorization_ref="OFFLINE-CAPTURE-NOT-LIVE-AUTHORIZATION",
            source_commit="a" * 40,
            sender=original.to[0],
            reply_mailbox=original.to[0],
            recipient=original.sender,
            participant_approval_ref="offline-fixture",
            sender_verification_ref="offline-fixture",
            expires_at=at + timedelta(minutes=10),
            max_requests=3,
            max_messages=1,
            max_received_messages=1,
            max_usd=Decimal("0.03"),
            request_usd_ceiling=Decimal("0.01"),
            price_source_ref="offline-fixture",
            price_source_at=at,
        )
        grant = install_email_live_grant(
            db,
            tenant_id=ctx.tenant_id,
            initiator_id=ctx.actor_id,
            connection_id=email.id,
            storage_connection_id=lane.storage_id,
            account_id="offline-account",
            credential_revision="offline-revision",
            config=config,
            retention=ApprovedEmailRetention(
                approval_ref="OFFLINE-RETENTION-FIXTURE",
                jurisdiction="offline",
                subject="synthetic participant",
                purpose="offline original capture proof",
                reviewer_id=ctx.actor_id,
                approved_at=at,
                effective_until=at + timedelta(hours=1),
                retention_days=1,
                max_bytes=1048576,
            ),
        )
        db.commit()
        email_id, policy_id = email.id, grant.retention_policy_id
        cap = ProviderIngressCapability(
            tenant_id=ctx.tenant_id,
            connection_id=email_id,
            credential_revision="offline-revision",
            inbox_addresses=original.to,
            webhook_secret_sha256=sha256(lane.secret.encode()).hexdigest(),
            retention_policy_id=policy_id,
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
        barriers: list[UUID] = []
        audited = ArtifactService._audit

        async def audit(
            self: ArtifactService,
            store: AsyncSession,
            context: StorageInvocationContext,
            action: str,
            target: UUID,
            details: dict[str, str | bool] | None = None,
        ) -> None:
            await audited(self, store, context, action, target, details)
            if late_kill and action == "artifact.capture_intent":

                def kill_after_commit(session: Session) -> None:
                    nonlocal killed
                    killed = True
                    barriers.append(target)

                event.listen(store.sync_session, "after_commit", kill_after_commit, once=True)

        monkeypatch.setattr(ArtifactService, "_audit", audit)
        calls: list[str] = []
        native = str(uuid4())

        def transport(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.host)
            if request.url.host == "api.resend.com":
                assert original.received_at is not None
                return httpx.Response(
                    200,
                    headers={"x-request-id": "offline-metadata"},
                    json={
                        "id": native,
                        "from": original.sender,
                        "to": list(original.to),
                        "subject": original.subject,
                        "text": original.text_body,
                        "html": None,
                        "created_at": original.received_at.isoformat(),
                        "message_id": original.message_id,
                        "headers": dict(original.headers),
                        "attachments": [],
                        "raw": {
                            "download_url": "https://source.resend.test/raw?secret=not-persisted",
                            "expires_at": (at + timedelta(hours=1)).isoformat(),
                        },
                    },
                )
            assert request.url.host == "source.resend.test" and "authorization" not in request.headers
            return httpx.Response(200, content=original.source.content)

        def port(
            material: ProviderIngressCapability, authority: Authority, observer: EmailRequestObserver
        ) -> EmailPort:
            return ResendRuntime(
                lambda tenant, connection: ResendBinding(
                    tenant_id=tenant,
                    connection_id=connection,
                    api_key=SecretStr("offline-no-key"),
                    inbox_addresses=material.inbox_addresses,
                    download_hosts=frozenset({"source.resend.test"}),
                    webhook_secret=SecretStr(lane.secret),
                    evidence_mode="LIVE",
                ),
                enabled=True,
                authority=authority,
                observer=observer,
                transport_factory=lambda: httpx.MockTransport(transport),
            ).bind(material.tenant_id, material.connection_id)

        runtime = ProviderIngressRuntime(
            app.state.business_sessions,
            lambda tenant, connection: cap,
            port,
            enabled=True,
            live_authorization=guard,
        )
        endpoint = uuid4()
        app.state.email_ingress = EmailIngressService(
            app.state.business_sessions,
            provider_ingress=runtime,
            shared_webhooks={
                endpoint: ResendWebhookRouter(
                    endpoint_id=endpoint, account_id="offline-account", secret=SecretStr(lane.secret)
                )
            },
        )
        db.rollback()
        activated = world.client.post(
            f"/api/workspaces/{ctx.tenant_id}/email-ingress/provider-activations",
            json={
                "email_connection_id": str(email_id),
                "storage_connection_id": str(lane.storage_id),
                "evidence": "Explicit offline approved capture fixture, never a live approval",
            },
        )
        assert activated.status_code == 200, activated.text
        payload = json.dumps(
            {
                "type": "email.received",
                "created_at": at.isoformat(),
                "data": {
                    "email_id": native,
                    "from": original.sender,
                    "to": list(original.to),
                },
            }
        )
        event_id = "msg_" + str(uuid4())
        response = world.client.post(
            f"/api/email-webhooks/{endpoint}",
            content=payload,
            headers={
                "svix-id": event_id,
                "svix-timestamp": str(int(at.timestamp())),
                "svix-signature": Webhook(lane.secret).sign(event_id, at, payload),
            },
        )
        assert response.status_code == 202, response.text
        receipt_id = UUID(response.json()[0]["id"])
        settings = ingress_settings(world.url, lane.secret).model_copy(
            update={"synthetic_email_ingress_enabled": False}
        )
        worker = WorkerRuntime(
            settings,
            ingress_producer=None,
            provider_ingress_factory=lambda sessions: ProviderIngressRuntime(
                sessions, lambda tenant, connection: cap, port, enabled=True, live_authorization=guard
            ),
        )
        try:
            async with worker.app.open_async():
                await dispatch_email_ingress(worker.sessions, worker.app)
                await worker.app.run_worker_async(
                    queues=["aco"], concurrency=1, wait=False, listen_notify=False
                )
            db.rollback()
            receipt = db.get(EmailIngressReceipt, receipt_id)
            assert receipt is not None
            assert receipt.state == ("failed" if late_kill else "review_required"), receipt.error_code
            assert calls == ["api.resend.com", "source.resend.test"]
            artifact = db.scalar(select(Artifact))
            assert (
                artifact is not None
                and artifact.capture_class == "real"
                and artifact.retention_policy_id == policy_id
            )
            assert artifact.storage_state == ("pending" if late_kill else "stored")
            assert receipt.agent_run_id is None  # Existing producer remains explicitly synthetic-only.
            if late_kill:
                assert barriers == [artifact.id] and artifact.object_version is None
                assert (
                    db.scalar(select(UsageAttempt).where(UsageAttempt.operation == "put_private_artifact"))
                    is None
                )
                assert db.scalar(select(CommunicationSource)) is None
            else:
                assert artifact.sha256 == original.source.integrity_sha256
                assert artifact.expires_at <= at + timedelta(hours=1)
                message = db.scalar(select(Communication))
                assert message is not None and message.synthetic is False and message.source_state == "stored"
                source = db.scalar(select(CommunicationSource))
                assert source is not None and source.evidence["synthetic"] is False
                assert source.evidence["provider_evidence_mode"] == "LIVE"
            attempts = list(
                db.scalars(select(UsageAttempt).where(UsageAttempt.provider_connection_id == email_id))
            )
            assert len(attempts) == 2 and all(row.event_id is not None for row in attempts)
            reservations = list(db.scalars(select(BudgetReservation)))
            assert len(reservations) == 2
            assert {row.operation_key for row in reservations} == {
                f"email-live:{grant.id}:{row.id}" for row in attempts
            }
            assert all(row.status == "UNKNOWN_SIDE_EFFECT" for row in reservations)
            usage = list(
                db.scalars(
                    select(ProviderUsageEvent).where(ProviderUsageEvent.provider_connection_id == email_id)
                )
            )
            assert len(usage) == 2 and all(not row.synthetic for row in usage)
        finally:
            await worker.engine.dispose()
