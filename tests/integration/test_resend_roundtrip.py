"""Actual maintained outbound/shared-reply round trip; all email HTTP is MOCKED_HTTP.

Only canonical accounting/policy/reminder fixtures and real private APP storage
are used. This is not live email, sender-domain verification or customer consent.
"""

import asyncio
import base64
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage as MimeMessage
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI
from pydantic import SecretStr
from reminder_support import reminder_world
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient
from svix.webhooks import Webhook

from aco.core.context import TenantContext
from aco.features.accounting.models import CustomerContact, InvoiceSnapshot
from aco.features.actions.models import OutboxEvent, ScheduledAction
from aco.features.agent.graph import dispatch_agent_runs
from aco.features.agent.models import AgentRun
from aco.features.agent.schemas import InvestigationResult
from aco.features.artifacts.models import RetentionPolicy
from aco.features.communications.activation import latest
from aco.features.communications.ingress import EmailIngressService, dispatch_email_ingress
from aco.features.communications.ingress_dto import ActivateIngress
from aco.features.communications.models import (
    Communication,
    CommunicationInvoiceLink,
    CommunicationRfcIdentity,
    CommunicationSource,
    EmailIngressReceipt,
    EmailSubmission,
    EmailWebhookAdmission,
)
from aco.features.communications.outbound import ReminderDispatcher
from aco.features.communications.provider_ingress import (
    Authority,
    ProviderIngressCapability,
    ProviderIngressRuntime,
)
from aco.features.communications.provider_outbound import ProviderOutboundCapability
from aco.features.identity.models import ProviderConnection, now
from aco.features.policies.models import RecipientControl
from aco.features.review.models import CollectionTask
from aco.features.usage.models import ProviderUsageEvent, UsageAttempt, UsageRevision
from aco.integrations.ports.email import EmailPort
from aco.integrations.ports.email_dto import EmailContent
from aco.integrations.ports.email_observer import EmailRequestObserver
from aco.integrations.resend.outbound import compose_outbound
from aco.integrations.resend.routing import ResendWebhookRouter
from aco.integrations.resend.runtime import ResendRuntime
from aco.integrations.resend.settings import ResendBinding
from aco.worker.app import WorkerRuntime
from aco.worker.jobs import DomainExecutor


def test_reviewed_resend_dispatch_signed_shared_reply_retains_thread_private_source_and_fake_task() -> None:
    from ingress_support import ingress_settings

    with reminder_world() as (client, store, ctx, _invoices, _saved):
        assert (
            isinstance(client, TestClient) and isinstance(store, Session) and isinstance(ctx, TenantContext)
        )
        app = client.app
        assert isinstance(app, FastAPI)
        # Keep real current-time recipient guards in their allowed local hours.
        offset = 12 - datetime.now(UTC).hour
        zone = f"Etc/GMT{-offset:+d}" if offset else "Etc/GMT"
        for contact in store.scalars(
            select(CustomerContact).where(CustomerContact.tenant_id == ctx.tenant_id)
        ):
            control = store.scalar(select(RecipientControl).where(RecipientControl.contact_id == contact.id))
            assert control is not None
            response = client.put(
                f"/api/workspaces/{ctx.tenant_id}/policy-recipients/{contact.id}",
                json={
                    "expected_version": control.version,
                    "expected_contact_revision": contact.source_revision,
                    "expected_destination": contact.email,
                    "timezone": zone,
                    "timezone_verified": True,
                    "owner_confirmed_tenant_timezone": False,
                    "destination_verified": True,
                    "allowed_channels": ["email"],
                    "opt_out": False,
                    "evidence": "Owner reviewed current synthetic noon-zone for offline round-trip only.",
                },
            )
            assert response.status_code == 200, response.text
        prior = latest(store, ctx.tenant_id)
        assert prior is not None
        prior_id, sender, destinations, storage_id = (
            prior.id,
            prior.sender,
            tuple(prior.destinations),
            prior.storage_connection_id,
        )
        email = ProviderConnection(
            tenant_id=ctx.tenant_id,
            provider="resend",
            provider_type="email",
            status="READY",
            encrypted_credentials_ref="offline-roundtrip-1",
            external_account_id="offline-roundtrip-account",
        )
        retention = RetentionPolicy(
            tenant_id=ctx.tenant_id,
            policy_key="synthetic_30_days",
            synthetic=True,
            retention_days=30,
            authority="synthetic_only_no_real_capture_authority",
        )
        store.add_all([email, retention])
        store.commit()
        secret = "whsec_" + base64.b64encode(b"offline-resend-roundtrip-only-key").decode()
        incoming_cap = ProviderIngressCapability(
            tenant_id=ctx.tenant_id,
            connection_id=email.id,
            credential_revision="offline-roundtrip-1",
            inbox_addresses=(sender,),
            webhook_secret_sha256=sha256(secret.encode()).hexdigest(),
            retention_policy_id=retention.id,
            retention_policy_version=retention.version,
        )
        wire: list[tuple[str, str]] = []
        outgoing_native, incoming_native = uuid4(), uuid4()
        observed_rfc = f"<{outgoing_native}@observed.resend.test>"
        incoming_metadata: dict[str, object] = {}
        incoming_original = b""

        def transport(request: httpx.Request) -> httpx.Response:
            wire.append((request.method, str(request.url)))
            if request.method == "POST":
                assert str(request.url) == "https://api.resend.com/emails"
                assert request.headers["idempotency-key"].startswith(f"aco:{ctx.tenant_id}:{email.id}:")
                return httpx.Response(
                    200, json={"id": str(outgoing_native)}, headers={"x-request-id": "offline-roundtrip-send"}
                )
            if request.url.path == f"/emails/{outgoing_native}":
                assert request.method == "GET"
                return httpx.Response(
                    200,
                    json={
                        "id": str(outgoing_native),
                        "from": sender,
                        "to": list(destinations),
                        "created_at": now().isoformat(),
                        "message_id": observed_rfc,
                    },
                    headers={"x-request-id": "offline-roundtrip-sent-read"},
                )
            if request.url.host == "api.resend.com":
                assert request.method == "GET" and request.url.path == f"/emails/receiving/{incoming_native}"
                return httpx.Response(
                    200, json=incoming_metadata, headers={"x-request-id": "offline-roundtrip-read"}
                )
            assert (
                request.method == "GET"
                and str(request.url) == f"https://source.resend.test/{incoming_native}.eml"
            )
            assert "authorization" not in request.headers
            return httpx.Response(200, content=incoming_original)

        def ingress_resolve(tenant: UUID, connection: UUID) -> ProviderIngressCapability:
            assert (tenant, connection) == (incoming_cap.tenant_id, incoming_cap.connection_id)
            return incoming_cap

        def ingress_port(
            cap: ProviderIngressCapability, authority: Authority, observer: EmailRequestObserver
        ) -> EmailPort:
            return ResendRuntime(
                lambda tenant, connection: ResendBinding(
                    tenant_id=tenant,
                    connection_id=connection,
                    api_key=SecretStr("offline-not-a-key"),
                    inbox_addresses=cap.inbox_addresses,
                    download_hosts=frozenset({"source.resend.test"}),
                    webhook_secret=SecretStr(secret),
                    evidence_mode="MOCKED_HTTP",
                ),
                authority=authority,
                observer=observer,
                enabled=True,
                transport_factory=lambda: httpx.MockTransport(transport),
            ).bind(cap.tenant_id, cap.connection_id)

        ingress = ProviderIngressRuntime(
            app.state.business_sessions, ingress_resolve, ingress_port, enabled=True
        )
        activated = ingress.activate(
            store,
            ctx,
            ActivateIngress(
                email_connection_id=email.id,
                storage_connection_id=storage_id,
                evidence="Current Owner reviewed explicitly mocked shared reply mailbox and private storage.",
            ),
        )
        store.commit()
        outbound_cap = ProviderOutboundCapability(
            tenant_id=ctx.tenant_id,
            connection_id=email.id,
            credential_revision="offline-roundtrip-1",
            account_id="offline-roundtrip-account",
            ingress_activation_id=activated.id,
            sender=sender,
            destinations=destinations,
            request_cap=3,
            observe_sent_identity=True,
        )
        outbound = compose_outbound(
            app.state.business_sessions,
            app.state.settings,
            ingress,
            lambda tenant, connection: outbound_cap,
            lambda cap: ResendBinding(
                tenant_id=cap.tenant_id,
                connection_id=cap.connection_id,
                api_key=SecretStr("offline-not-a-key"),
                inbox_addresses=(sender,),
                download_hosts=frozenset({"source.resend.test"}),
                webhook_secret=SecretStr(secret),
                evidence_mode="MOCKED_HTTP",
            ),
            transport_factory=lambda: httpx.MockTransport(transport),
            enabled=True,
        )
        app.state.ports = replace(app.state.ports, email=outbound)
        review_path = f"/api/workspaces/{ctx.tenant_id}/email-execution/provider"
        exposed = client.get(review_path)
        assert exposed.status_code == 200, exposed.text
        configured = exposed.json()["configurations"][0]
        review = {
            "reviewed_hash": configured["reviewed_hash"],
            "connection_id": str(email.id),
            "expected_activation_id": str(prior_id),
            "evidence": "Owner reviewed mocked sender, recipients, cap and private capture.",
        }
        assert client.put(review_path, json=review, headers={"x-csrf-token": "invalid"}).status_code == 403
        response = client.put(review_path, json=review)
        assert response.status_code == 200, response.text
        assert response.json()["activation"]["email_connection_id"] == str(email.id)
        assert response.json()["activation"]["current"] is True
        endpoint = uuid4()
        service = EmailIngressService(
            app.state.business_sessions,
            provider_ingress=ingress,
            shared_webhooks={
                endpoint: ResendWebhookRouter(
                    endpoint_id=endpoint, account_id="offline-roundtrip-account", secret=SecretStr(secret)
                )
            },
        )
        app.state.email_ingress = service
        selected = store.scalar(
            select(ScheduledAction)
            .where(ScheduledAction.tenant_id == ctx.tenant_id, ScheduledAction.status == "queued")
            .order_by(ScheduledAction.created_at.desc())
        )
        assert selected is not None
        selected.due_at = now()
        outbox = store.scalar(
            select(OutboxEvent).where(
                OutboxEvent.action_id == selected.id,
                OutboxEvent.action_version == selected.version,
                OutboxEvent.dispatched_at.is_(None),
            )
        )
        assert outbox is not None
        outbox.due_at = selected.due_at
        action_id, invoice_id = selected.id, selected.invoice_id
        invoice = store.get(InvoiceSnapshot, invoice_id)
        assert invoice is not None
        financial = (invoice.balance_minor, invoice.financial_status, invoice.source_revision)
        store.commit()
        url = store.get_bind().engine.url.render_as_string(hide_password=False)
        store.rollback()

        async def execute() -> None:
            nonlocal incoming_metadata, incoming_original
            dispatcher = ReminderDispatcher(
                app.state.business_sessions, app.state.settings, outbound, app.state.ports.storage
            )
            worker = WorkerRuntime(
                ingress_settings(url, secret).model_copy(update={"synthetic_email_ingress_enabled": False}),
                DomainExecutor(dispatcher),
                provider_ingress_factory=lambda sessions: ProviderIngressRuntime(
                    sessions, ingress_resolve, ingress_port, enabled=True
                ),
            )
            try:
                async with worker.app.open_async():
                    for _ in range(20):
                        await worker.dispatcher.dispatch_outbox(100)
                        async with worker.sessions() as db:
                            action = await db.get(ScheduledAction, action_id)
                            assert action is not None
                            if action.job_id is not None:
                                break
                    assert action is not None and action.job_id is not None
                    await worker.app.run_worker_async(
                        queues=["aco"],
                        concurrency=1,
                        wait=False,
                        listen_notify=False,
                        install_signal_handlers=False,
                    )
                    store.rollback()
                    action = store.get(ScheduledAction, action_id, populate_existing=True)
                    assert action is not None and action.status == "succeeded", (
                        action.last_error if action else None
                    )
                    submission = store.scalar(select(EmailSubmission))
                    assert submission is not None and submission.state == "accepted"
                    assert submission.provider_message_id == "sent:" + str(outgoing_native)
                    sent = store.scalar(
                        select(Communication).where(Communication.operation_id == submission.operation_id)
                    )
                    assert (
                        sent is not None
                        and sent.source_state == "stored"
                        and sent.delivery_state == "accepted"
                    )
                    sent_id, thread_id = sent.id, sent.thread_id
                    sent_content = EmailContent.model_validate_json(json.dumps(sent.content))
                    assert len(sent_content.to) == 1
                    rfc_id, reply_sender = sent_content.message_id, sent_content.to[0]
                    assert isinstance(rfc_id, str) and isinstance(reply_sender, str)
                    assert rfc_id != observed_rfc
                    alias = store.scalar(select(CommunicationRfcIdentity))
                    assert alias is not None and alias.rfc_message_id == observed_rfc
                    assert alias.communication_id == sent.id and alias.submission_id == submission.id
                    composed = store.scalar(
                        select(CommunicationSource).where(CommunicationSource.communication_id == sent.id)
                    )
                    assert composed is not None and composed.representation == "ACO_COMPOSED"
                    composed_id, composed_hash = composed.artifact_id, composed.sha256
                    store.rollback()
                    original_response = client.get(
                        f"/api/workspaces/{ctx.tenant_id}/artifacts/{composed_id}/content"
                    )
                    assert (
                        original_response.status_code == 200
                        and sha256(original_response.content).hexdigest() == composed_hash
                    )
                    assert rfc_id.encode() in original_response.content
                    assert observed_rfc.encode() not in original_response.content
                    at = now()
                    mime = MimeMessage()
                    mime["From"], mime["To"], mime["Subject"] = (
                        reply_sender,
                        sender,
                        "Re: Explicit synthetic collection reply",
                    )
                    mime["Message-ID"] = f"<{incoming_native}@synthetic.invalid>"
                    mime["In-Reply-To"], mime["References"] = observed_rfc, observed_rfc
                    body = (
                        "SYNTHETIC source reply with no invoice number: "
                        "Please investigate the prior invoice conversation."
                    )
                    mime.set_content(body)
                    incoming_original = mime.as_bytes()
                    incoming_metadata = {
                        "id": str(incoming_native),
                        "from": reply_sender,
                        "to": [sender],
                        "subject": str(mime["Subject"]),
                        "text": body,
                        "html": None,
                        "created_at": at.isoformat(),
                        "message_id": str(mime["Message-ID"]),
                        "headers": dict(mime.items()),
                        "attachments": [],
                        "raw": {
                            "download_url": f"https://source.resend.test/{incoming_native}.eml",
                            "expires_at": (at + timedelta(hours=1)).isoformat(),
                        },
                    }
                    raw = json.dumps(
                        {
                            "type": "email.received",
                            "created_at": at.isoformat(),
                            "data": {"email_id": str(incoming_native), "to": [sender]},
                        }
                    ).encode()
                    event = "msg_roundtrip_" + uuid4().hex
                    headers = {
                        "svix-id": event,
                        "svix-timestamp": str(int(at.timestamp())),
                        "svix-signature": Webhook(secret).sign(event, at, raw.decode()),
                    }
                    path = f"/api/email-webhooks/{endpoint}"
                    callback = client.post(path, content=raw, headers=headers)
                    assert callback.status_code == 202, callback.text
                    receipt_id = UUID(callback.json()[0]["id"])
                    replay = client.post(path, content=raw, headers=headers)
                    assert replay.status_code == 202 and replay.json()[0]["id"] == str(receipt_id)
                    store.rollback()
                    await dispatch_email_ingress(worker.sessions, worker.app)
                    await worker.app.run_worker_async(
                        queues=["aco"],
                        concurrency=1,
                        wait=False,
                        listen_notify=False,
                        install_signal_handlers=False,
                    )
                    store.rollback()
                    receipt = store.get(EmailIngressReceipt, receipt_id)
                    assert receipt is not None and receipt.state == "processed", (
                        receipt.error_code if receipt else None
                    )
                    assert receipt.agent_run_id is not None and receipt.communication_id is not None
                    received = store.get(Communication, receipt.communication_id)
                    assert received is not None and received.id != sent_id and received.thread_id == thread_id
                    assert (
                        received.provider_connection_id == email.id
                        and received.origin_invoice_id == invoice_id
                    )
                    assert received.source_state == "stored" and received.synthetic
                    assert received.content["in_reply_to"] == observed_rfc
                    assert rfc_id.encode() not in incoming_original
                    links = list(
                        store.scalars(
                            select(CommunicationInvoiceLink).where(
                                CommunicationInvoiceLink.communication_id == received.id
                            )
                        )
                    )
                    assert [link.invoice_id for link in links] == [invoice_id]
                    assert (
                        links[0].evidence_excerpt == ""
                        and links[0].confidence == "contextual_unverified_sender"
                    )
                    original = store.scalar(
                        select(CommunicationSource).where(CommunicationSource.communication_id == received.id)
                    )
                    assert original is not None and original.representation == "provider_original"
                    artifact_id = original.artifact_id
                    store.rollback()
                    captured = client.get(f"/api/workspaces/{ctx.tenant_id}/artifacts/{artifact_id}/content")
                    assert captured.status_code == 200 and captured.content == incoming_original
                    assert sha256(captured.content).hexdigest() == sha256(incoming_original).hexdigest()
                    store.rollback()
                    await dispatch_agent_runs(worker.sessions, worker.app)
                    await worker.app.run_worker_async(
                        queues=["aco"],
                        concurrency=1,
                        wait=False,
                        listen_notify=False,
                        install_signal_handlers=False,
                    )
                    store.rollback()
                    run = store.get(AgentRun, receipt.agent_run_id, populate_existing=True)
                    assert run is not None and run.authenticity == "FAKE" and not run.provider_attempted
                    assert run.status == "completed" and run.source_communication_id == received.id, (
                        run.error_code
                    )
                    assert run.result is not None
                    result = InvestigationResult.model_validate_json(json.dumps(run.result))
                    changes = [
                        key
                        for proposal in result.proposals
                        if proposal.kind == "create_task" and proposal.executed
                        for key in proposal.changed_ids
                    ]
                    assert len(changes) == 1
                    task = store.get(CollectionTask, changes[0])
                    assert task is not None and task.tenant_id == ctx.tenant_id
                    store.rollback()
                    # Re-admit exact signed event and run maintenance: no duplicated effects.
                    replay = client.post(path, content=raw, headers=headers)
                    assert replay.status_code == 202 and replay.json()[0]["id"] == str(receipt_id)
                    await dispatch_email_ingress(worker.sessions, worker.app)
                    await dispatch_agent_runs(worker.sessions, worker.app)
                    await worker.dispatcher.dispatch_outbox(100)
                    await worker.app.run_worker_async(
                        queues=["aco"],
                        concurrency=1,
                        wait=False,
                        listen_notify=False,
                        install_signal_handlers=False,
                    )
            finally:
                await worker.engine.dispose()

        asyncio.run(execute())
        store.rollback()
        assert [method for method, _url in wire] == ["POST", "GET", "GET", "GET"]
        assert wire[1] == ("GET", f"https://api.resend.com/emails/{outgoing_native}")
        assert store.scalar(select(func.count()).select_from(CommunicationRfcIdentity)) == 1
        assert store.scalar(select(func.count()).select_from(EmailSubmission)) == 1
        assert store.scalar(select(func.count()).select_from(Communication)) == 2
        assert store.scalar(select(func.count()).select_from(CommunicationSource)) == 2
        assert store.scalar(select(func.count()).select_from(EmailIngressReceipt)) == 1
        assert store.scalar(select(func.count()).select_from(EmailWebhookAdmission)) == 1
        assert store.scalar(select(func.count()).select_from(AgentRun)) == 1
        assert store.scalar(select(func.count()).select_from(CollectionTask)) == 1
        email_events = list(
            store.scalars(
                select(ProviderUsageEvent).where(ProviderUsageEvent.provider_connection_id == email.id)
            )
        )
        assert len(email_events) == 4 and all(
            event.provider == "resend" and event.synthetic for event in email_events
        )
        observed = [event for event in email_events if event.operation == "email_submission_get_message"]
        assert len(observed) == 1
        assert observed[0].native_subject == "sent:" + str(outgoing_native)
        assert observed[0].native_operation_id == "offline-roundtrip-sent-read"
        attempt = store.scalar(select(UsageAttempt).where(UsageAttempt.event_id == observed[0].id))
        assert attempt is not None and attempt.outcome == "SUCCEEDED"
        assert attempt.business_outcome == "OBSERVED"
        for event in email_events:
            revision = store.scalar(
                select(UsageRevision)
                .where(UsageRevision.event_id == event.id)
                .order_by(UsageRevision.version.desc())
            )
            assert (
                revision is not None and revision.cost_amount is None and revision.cost_quality == "UNKNOWN"
            )
        reloaded = store.get(InvoiceSnapshot, invoice_id, populate_existing=True)
        assert (
            reloaded is not None
            and (reloaded.balance_minor, reloaded.financial_status, reloaded.source_revision) == financial
        )
