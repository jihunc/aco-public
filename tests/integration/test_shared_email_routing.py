"""Shared-account ingress isolation; all provider HTTP is explicitly offline."""

from hashlib import sha256
from typing import cast
from uuid import UUID

import pytest
from accounting_support import context
from fastapi import FastAPI, HTTPException
from identity_support import actor_client, add_actor
from sqlalchemy.orm import Session

from aco.features.artifacts.models import RetentionPolicy
from aco.features.communications.ingress_dto import ActivateIngress
from aco.features.communications.models import EmailIngressActivation
from aco.features.communications.provider_ingress import ProviderIngressCapability, ProviderIngressRuntime
from aco.features.identity.models import Membership, ProviderConnection, Tenant
from aco.integrations.ports.email import EmailPort


def material(db: Session, tenant: UUID, address: str, secret: str) -> tuple[ProviderIngressCapability, UUID]:
    email = ProviderConnection(
        tenant_id=tenant,
        provider="resend",
        provider_type="email",
        status="READY",
        external_account_id="shared-resend-test-account",
        encrypted_credentials_ref="offline-key-revision-1",
    )
    storage = ProviderConnection(tenant_id=tenant, provider="s3", provider_type="storage", status="READY")
    retention = RetentionPolicy(
        tenant_id=tenant,
        policy_key="synthetic_30_days",
        synthetic=True,
        retention_days=30,
        authority="synthetic_only_no_real_capture_authority",
    )
    db.add_all([email, storage, retention])
    db.commit()
    return ProviderIngressCapability(
        tenant_id=tenant,
        connection_id=email.id,
        credential_revision="offline-key-revision-1",
        inbox_addresses=(address,),
        webhook_secret_sha256=sha256(secret.encode()).hexdigest(),
        retention_policy_id=retention.id,
        retention_policy_version=retention.version,
    ), storage.id


def test_provider_mailbox_has_one_active_owner_across_independent_tenants() -> None:
    with actor_client("owner") as (client, db, owner, tenant):
        app = cast(FastAPI, client.app)
        other_user, _other_client = add_actor(db, app, "other-mailbox-owner@example.test")
        other = Tenant(name="Independent mailbox owner", timezone="UTC")
        db.add(other)
        db.flush()
        db.add(Membership(tenant_id=other.id, user_id=other_user.id, role="owner"))
        db.commit()
        first, storage_a = material(db, tenant, "company-a@aco.example.com", "offline-shared-webhook-key")
        second, storage_b = material(db, other.id, "company-a@aco.example.com", "offline-shared-webhook-key")
        capabilities = {first.connection_id: first, second.connection_id: second}

        def resolve(tenant_id: UUID, connection_id: UUID) -> ProviderIngressCapability:
            result = capabilities[connection_id]
            assert result.tenant_id == tenant_id
            return result

        def forbidden(*args: object) -> EmailPort:
            raise AssertionError("Activation cannot fetch private messages")

        runtime = ProviderIngressRuntime(app.state.business_sessions, resolve, forbidden, enabled=True)
        active = runtime.activate(
            db,
            context(tenant, owner.id),
            ActivateIngress(
                email_connection_id=first.connection_id,
                storage_connection_id=storage_a,
                evidence="Explicit first synthetic mailbox ownership.",
            ),
        )
        db.commit()
        with pytest.raises(HTTPException) as denied:
            runtime.activate(
                db,
                context(other.id, other_user.id),
                ActivateIngress(
                    email_connection_id=second.connection_id,
                    storage_connection_id=storage_b,
                    evidence="Conflicting second synthetic mailbox ownership.",
                ),
            )
        assert denied.value.status_code == 409
        assert denied.value.detail == "INBOUND_MAILBOX_ALREADY_OWNED"
        db.rollback()
        stored = db.get(EmailIngressActivation, active.id)
        assert stored is not None
        assert runtime.validate(db, stored).tenant_id == tenant


async def test_shared_account_routes_signed_recipients_and_replays_without_activation_id() -> None:
    import base64
    import json
    from dataclasses import replace
    from datetime import timedelta
    from email.message import EmailMessage as MimeMessage
    from uuid import uuid4

    import httpx
    from accounting_support import seed
    from artifact_support import storage_configuration
    from ingress_support import ingress_settings
    from pydantic import SecretStr
    from sqlalchemy import Engine, func, select
    from svix.webhooks import Webhook

    from aco.features.accounting.models import CustomerContact, InvoiceSnapshot
    from aco.features.agent.graph import dispatch_agent_runs
    from aco.features.agent.models import AgentRun
    from aco.features.communications.ingress import EmailIngressService, dispatch_email_ingress
    from aco.features.communications.models import (
        Communication,
        CommunicationInvoiceLink,
        CommunicationSource,
        EmailIngressReceipt,
        EmailWebhookAdmission,
    )
    from aco.features.communications.provider_ingress import Authority
    from aco.features.identity.models import now
    from aco.features.review.models import CollectionTask
    from aco.integrations.ports.email_observer import EmailRequestObserver
    from aco.integrations.resend.routing import ResendWebhookRouter
    from aco.integrations.resend.runtime import ResendRuntime
    from aco.integrations.resend.settings import ResendBinding
    from aco.integrations.s3.adapter import StorageRuntime
    from aco.worker.app import WorkerRuntime

    with actor_client("owner") as (client, db, owner, tenant):
        app = cast(FastAPI, client.app)
        other_user, other_client = add_actor(db, app, "second-shared-owner@example.test")
        other = Tenant(name="Shared domain second tenant", timezone="UTC")
        db.add(other)
        db.flush()
        db.add(Membership(tenant_id=other.id, user_id=other_user.id, role="owner"))
        db.add(ProviderConnection(tenant_id=other.id, provider="demo", provider_type="accounting"))
        db.commit()
        seed(client, tenant)
        seed(other_client, other.id)
        invoice_scope: dict[UUID, UUID] = {}
        sender_scope: dict[UUID, str] = {}
        financial_before = {}
        for owner_tenant in (tenant, other.id):
            invoice = db.scalar(
                select(InvoiceSnapshot).where(
                    InvoiceSnapshot.tenant_id == owner_tenant,
                    InvoiceSnapshot.number == "INV-101",
                )
            )
            assert invoice is not None
            contact = db.scalar(
                select(CustomerContact).where(
                    CustomerContact.tenant_id == owner_tenant,
                    CustomerContact.customer_id == invoice.customer_id,
                )
            )
            assert contact is not None and contact.email is not None
            invoice_scope[owner_tenant] = invoice.id
            sender_scope[owner_tenant] = contact.email
            financial_before[invoice.id] = (
                invoice.balance_minor,
                invoice.financial_status,
                invoice.source_revision,
            )
        db.rollback()
        secret = "whsec_" + base64.b64encode(b"offline-shared-account-webhook-key").decode()
        cap_a, storage_a = material(db, tenant, "company-a@aco.example.com", secret)
        cap_b, storage_b = material(db, other.id, "company-b@aco.example.com", secret)
        caps = {cap_a.connection_id: cap_a, cap_b.connection_id: cap_b}
        http_calls: list[str] = []
        messages: dict[str, tuple[dict[str, object], bytes]] = {}
        app.state.ports = replace(app.state.ports, storage=StorageRuntime(**storage_configuration()))

        def resolve(tenant_id: UUID, connection_id: UUID) -> ProviderIngressCapability:
            cap = caps[connection_id]
            assert cap.tenant_id == tenant_id
            return cap

        def forbidden_http(request: httpx.Request) -> httpx.Response:
            http_calls.append(request.method)
            if request.url.host == "api.resend.com":
                native = request.url.path.rsplit("/", 1)[-1]
                return httpx.Response(200, json=messages[native][0])
            assert request.url.host == "source.resend.test" and "authorization" not in request.headers
            return httpx.Response(200, content=messages[request.url.path.strip("/").removesuffix(".eml")][1])

        def factory(
            cap: ProviderIngressCapability, authority: Authority, observer: EmailRequestObserver
        ) -> EmailPort:
            return ResendRuntime(
                lambda tenant_id, connection_id: ResendBinding(
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                    api_key=SecretStr("offline-not-a-key"),
                    inbox_addresses=cap.inbox_addresses,
                    download_hosts=frozenset({"source.resend.test"}),
                    webhook_secret=SecretStr(secret),
                    evidence_mode="MOCKED_HTTP",
                ),
                authority=authority,
                observer=observer,
                enabled=True,
                transport_factory=lambda: httpx.MockTransport(forbidden_http),
            ).bind(cap.tenant_id, cap.connection_id)

        runtime = ProviderIngressRuntime(app.state.business_sessions, resolve, factory, enabled=True)
        active_a = runtime.activate(
            db,
            context(tenant, owner.id),
            ActivateIngress(
                email_connection_id=cap_a.connection_id,
                storage_connection_id=storage_a,
                evidence="Owner A reviews shared account receiving address.",
            ),
        )
        active_b = runtime.activate(
            db,
            context(other.id, other_user.id),
            ActivateIngress(
                email_connection_id=cap_b.connection_id,
                storage_connection_id=storage_b,
                evidence="Owner B reviews shared account receiving address.",
            ),
        )
        db.commit()
        endpoint = uuid4()
        service = EmailIngressService(
            app.state.business_sessions,
            provider_ingress=runtime,
            shared_webhooks={
                endpoint: ResendWebhookRouter(
                    endpoint_id=endpoint,
                    account_id="shared-resend-test-account",
                    secret=SecretStr(secret),
                )
            },
        )
        app.state.email_ingress = service
        path = f"/api/email-webhooks/{endpoint}"

        def notification(recipients: list[str], event: str | None = None) -> tuple[bytes, dict[str, str]]:
            at = now()
            native = str(uuid4())
            if len(recipients) == 1 and recipients[0] in {cap_a.inbox_addresses[0], cap_b.inbox_addresses[0]}:
                owner_cap = cap_a if recipients[0] == cap_a.inbox_addresses[0] else cap_b
                mime = MimeMessage()
                mime["From"] = sender_scope[owner_cap.tenant_id]
                mime["To"] = recipients[0]
                mime["Message-ID"] = "<same-rfc-reference@synthetic.invalid>"
                mime["Subject"] = "Explicit synthetic INV-101 source"
                body = "SYNTHETIC fixture mentions INV-101 only. Please investigate the invoice context."
                mime.set_content(body)
                messages[native] = (
                    {
                        "id": native,
                        "from": str(mime["From"]),
                        "to": recipients,
                        "subject": str(mime["Subject"]),
                        "text": body,
                        "html": None,
                        "created_at": at.isoformat(),
                        "message_id": str(mime["Message-ID"]),
                        "headers": dict(mime.items()),
                        "attachments": [],
                        "raw": {
                            "download_url": f"https://source.resend.test/{native}.eml",
                            "expires_at": (at + timedelta(hours=1)).isoformat(),
                        },
                    },
                    mime.as_bytes(),
                )
            raw = json.dumps(
                {
                    "type": "email.received",
                    "created_at": at.isoformat(),
                    "data": {
                        "email_id": native,
                        "to": recipients,
                        "tenant_id": str(other.id),
                        "activation_id": str(active_b.id),
                    },
                }
            ).encode()
            identity = event or f"msg_{uuid4()}"
            return raw, {
                "svix-id": identity,
                "svix-timestamp": str(int(at.timestamp())),
                "svix-signature": Webhook(secret).sign(identity, at, raw.decode()),
            }

        receipt_ids = []
        for cap in (cap_a, cap_b):
            raw, headers = notification(list(cap.inbox_addresses))
            response = client.post(path, content=raw, headers=headers)
            assert response.status_code == 202, response.text
            key = UUID(response.json()[0]["id"])
            receipt_ids.append(key)
            repeated = client.post(path, content=raw, headers=headers)
            assert repeated.status_code == 202 and repeated.json()[0]["id"] == str(key)
            db.rollback()
            receipt = db.get(EmailIngressReceipt, key)
            assert receipt is not None and receipt.tenant_id == cap.tenant_id
            assert receipt.email_connection_id == cap.connection_id
            db.rollback()
        for recipients, status in [
            (["unknown@aco.example.com"], 422),
            (["company-a@aco.example.com", "unknown@aco.example.com"], 422),
            (["company-a@aco.example.com", "company-b@aco.example.com"], 409),
        ]:
            raw, headers = notification(recipients)
            response = client.post(path, content=raw, headers=headers)
            assert response.status_code == status, response.text
        raw, headers = notification(["company-a@aco.example.com"])
        assert client.post(path, content=raw + b" ", headers=headers).status_code == 403
        assert (
            client.post(f"/api/email-ingress/{active_a.id}", content=raw, headers=headers).status_code == 403
        )
        db.rollback()
        assert db.scalar(select(func.count()).select_from(EmailIngressReceipt)) == 2
        assert db.scalar(select(func.count()).select_from(EmailWebhookAdmission)) == 2
        assert http_calls == []
        engine = db.get_bind()
        assert isinstance(engine, Engine)
        url = engine.url.render_as_string(hide_password=False)
        db.rollback()
        worker = WorkerRuntime(
            ingress_settings(url, secret).model_copy(update={"synthetic_email_ingress_enabled": False}),
            provider_ingress_factory=lambda sessions: ProviderIngressRuntime(
                sessions, resolve, factory, enabled=True
            ),
        )
        try:
            async with worker.app.open_async():
                await dispatch_email_ingress(worker.sessions, worker.app)
                await worker.app.run_worker_async(
                    queues=["aco"], concurrency=1, wait=False, listen_notify=False
                )
                db.rollback()
                for key, cap in zip(receipt_ids, (cap_a, cap_b), strict=True):
                    receipt = db.get(EmailIngressReceipt, key)
                    assert receipt is not None and receipt.state == "processed", (
                        receipt.error_code if receipt else None
                    )
                    assert receipt.agent_run_id is not None
                    communication = db.get(Communication, receipt.communication_id)
                    assert communication is not None and communication.tenant_id == cap.tenant_id
                    assert communication.source_state == "stored"
                    links = list(
                        db.scalars(
                            select(CommunicationInvoiceLink).where(
                                CommunicationInvoiceLink.communication_id == communication.id,
                            )
                        )
                    )
                    assert [link.invoice_id for link in links] == [invoice_scope[cap.tenant_id]]
                db.rollback()
                await dispatch_agent_runs(worker.sessions, worker.app)
                await worker.app.run_worker_async(
                    queues=["aco"], concurrency=1, wait=False, listen_notify=False
                )
        finally:
            await worker.engine.dispose()
        db.rollback()
        assert db.scalar(select(func.count()).select_from(Communication)) == 2
        assert db.scalar(select(func.count()).select_from(CommunicationSource)) == 2
        assert db.scalar(select(func.count()).select_from(AgentRun)) == 2
        for cap in (cap_a, cap_b):
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(CollectionTask)
                    .where(
                        CollectionTask.tenant_id == cap.tenant_id,
                    )
                )
                == 1
            )
        for invoice_id, expected in financial_before.items():
            invoice = db.get(InvoiceSnapshot, invoice_id)
            assert invoice is not None
            assert (invoice.balance_minor, invoice.financial_status, invoice.source_revision) == expected
        assert len(http_calls) == 4
