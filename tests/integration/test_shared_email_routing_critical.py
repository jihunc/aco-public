"""Independent shared-account replay and fetched-envelope controls; fake transport only."""

import base64
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from email.message import EmailMessage as MimeMessage
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
import pytest
from accounting_support import context
from fastapi import FastAPI, HTTPException
from identity_support import actor_client, add_actor
from integration.test_mailbox_ownership_critical import Owner
from integration.test_shared_email_routing import material
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from svix.webhooks import Webhook

from aco.core.types import ProviderReference
from aco.features.artifacts.models import RetentionPolicy
from aco.features.communications.ingress import EmailIngressService, current_activation
from aco.features.communications.models import Communication, EmailIngressReceipt, EmailWebhookAdmission
from aco.features.communications.provider_ingress import (
    Authority,
    ProviderIngressCapability,
    ProviderIngressRuntime,
)
from aco.features.identity.models import Membership, ProviderConnection, Tenant, now
from aco.integrations.ports.email import EmailPort
from aco.integrations.ports.email_context import EmailIngressContext, EmailWebhookContext
from aco.integrations.ports.email_dto import EmailMessage, EmailSource
from aco.integrations.ports.email_observer import EmailRequestObserver
from aco.integrations.resend.routing import ResendWebhookRouter
from aco.integrations.resend.runtime import ResendRuntime
from aco.integrations.resend.settings import ResendBinding


@dataclass
class SharedWorld:
    db: Session
    runtime: ProviderIngressRuntime
    service: EmailIngressService
    owners: tuple[Owner, Owner]
    caps: dict[UUID, ProviderIngressCapability]
    endpoints: tuple[UUID, UUID]
    secret: str

    def notification(self, address: str) -> tuple[bytes, dict[str, str]]:
        at = now()
        event = "msg_" + uuid4().hex
        raw = json.dumps(
            {
                "type": "email.received",
                "created_at": at.isoformat(),
                "data": {"email_id": str(uuid4()), "to": [address]},
            }
        ).encode()
        return raw, {
            "svix-id": event,
            "svix-timestamp": str(int(at.timestamp())),
            "svix-signature": Webhook(self.secret).sign(event, at, raw.decode()),
        }


@contextmanager
def shared_world() -> Iterator[SharedWorld]:
    from typing import cast

    with actor_client("owner") as (client, db, user, tenant):
        app = cast(FastAPI, client.app)
        other_user, _ = add_actor(db, app, "independent-shared-replay@example.test")
        other = Tenant(name="Independent shared replay recipient", timezone="UTC")
        db.add(other)
        db.flush()
        db.add(Membership(tenant_id=other.id, user_id=other_user.id, role="owner"))
        db.commit()
        secret = "whsec_" + base64.b64encode(b"independent-shared-replay-test-key").decode()
        first, storage_a = material(db, tenant, "shared@synthetic.invalid", secret)
        second, storage_b = material(db, other.id, "second@synthetic.invalid", secret)
        caps = {first.connection_id: first, second.connection_id: second}

        def resolve(tenant_id: UUID, connection_id: UUID) -> ProviderIngressCapability:
            cap = caps[connection_id]
            assert isinstance(cap, ProviderIngressCapability)
            assert cap.tenant_id == tenant_id
            return cap

        def no_http(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("Routing/replay controls must not fetch messages")

        def factory(
            cap: ProviderIngressCapability, authority: Authority, observer: EmailRequestObserver
        ) -> EmailPort:
            return ResendRuntime(
                lambda tid, cid: ResendBinding(
                    tenant_id=tid,
                    connection_id=cid,
                    api_key=SecretStr("offline-not-a-key"),
                    inbox_addresses=cap.inbox_addresses,
                    download_hosts=frozenset({"source.resend.test"}),
                    webhook_secret=SecretStr(secret),
                    evidence_mode="MOCKED_HTTP",
                ),
                authority=authority,
                observer=observer,
                enabled=True,
                transport_factory=lambda: httpx.MockTransport(no_http),
            ).bind(cap.tenant_id, cap.connection_id)

        runtime = ProviderIngressRuntime(app.state.business_sessions, resolve, factory, enabled=True)
        endpoints = (uuid4(), uuid4())
        service = EmailIngressService(
            app.state.business_sessions,
            provider_ingress=runtime,
            shared_webhooks={
                key: ResendWebhookRouter(
                    endpoint_id=key, account_id="shared-resend-test-account", secret=SecretStr(secret)
                )
                for key in endpoints
            },
        )
        yield SharedWorld(
            db,
            runtime,
            service,
            (
                Owner(context(tenant, user.id), first, storage_a),
                Owner(context(other.id, other_user.id), second, storage_b),
            ),
            caps,
            endpoints,
            secret,
        )


async def test_retained_pre_registry_receipt_cannot_be_admitted_under_reassigned_tenant() -> None:
    """Legacy source history predates the new global admission table, not the signature."""
    with shared_world() as lane:
        first, second = lane.owners
        active = lane.runtime.activate(lane.db, first.ctx, first.command())
        lane.db.commit()
        raw, headers = lane.notification(first.cap.inbox_addresses[0])
        old = current_activation(lane.db, active.id, first.ctx.tenant_id)
        ctx = EmailWebhookContext(
            tenant_id=old.tenant_id,
            actor_id=old.worker_actor_id,
            correlation_id=uuid4(),
            activation_id=old.id,
            email_connection_id=old.email_connection_id,
            raw_sha256=sha256(raw).hexdigest(),
        )
        adapter = lane.runtime.port(old, lane.db)
        lane.db.rollback()
        verified = await adapter.verify_webhook(ctx, raw, headers)
        # Actual verified canonical receipt path used by pre-r22 activation endpoints.
        # No receipt identity, notification or authority is manufactured by the fixture.
        async with lane.service.sessions.begin() as db:
            original = (
                await lane.service._admit_verified(db, ctx, raw, headers, verified.value, is_provider=True)
            )[0]
        lane.db.rollback()
        assert lane.db.scalar(select(func.count()).select_from(EmailWebhookAdmission)) == 0
        lane.db.rollback()
        lane.service.revoke(lane.db, first.ctx, active.id, active.version)
        lane.db.commit()
        lane.caps[second.cap.connection_id] = second.cap.model_copy(
            update={"inbox_addresses": first.cap.inbox_addresses}
        )
        reassigned = lane.runtime.activate(lane.db, second.ctx, second.command())
        lane.db.commit()
        assert reassigned.id != active.id
        with pytest.raises(HTTPException) as refused:
            await lane.service.receive_shared(lane.endpoints[1], raw, headers)
        assert refused.value.status_code == 409
        lane.db.rollback()
        records = list(lane.db.scalars(select(EmailIngressReceipt)))
        assert (
            len(records) == 1 and records[0].id == original.id and records[0].tenant_id == first.ctx.tenant_id
        )


async def test_new_shared_event_replay_is_fenced_across_endpoint_and_reassignment() -> None:
    with shared_world() as lane:
        first, second = lane.owners
        active = lane.runtime.activate(lane.db, first.ctx, first.command())
        lane.db.commit()
        raw, headers = lane.notification(first.cap.inbox_addresses[0])
        original = (await lane.service.receive_shared(lane.endpoints[0], raw, headers))[0]
        replay = (await lane.service.receive_shared(lane.endpoints[1], raw, headers))[0]
        assert replay.id == original.id
        lane.service.revoke(lane.db, first.ctx, active.id, active.version)
        lane.db.commit()
        lane.caps[second.cap.connection_id] = second.cap.model_copy(
            update={"inbox_addresses": first.cap.inbox_addresses}
        )
        lane.runtime.activate(lane.db, second.ctx, second.command())
        lane.db.commit()
        with pytest.raises(HTTPException) as refused:
            await lane.service.receive_shared(lane.endpoints[1], raw, headers)
        assert refused.value.status_code == 409
        lane.db.rollback()
        receipts = list(lane.db.scalars(select(EmailIngressReceipt)))
        assert (
            len(receipts) == 1
            and receipts[0].id == original.id
            and receipts[0].tenant_id == first.ctx.tenant_id
        )
        assert lane.db.scalar(select(func.count()).select_from(EmailWebhookAdmission)) == 1


@pytest.mark.parametrize("revocation", ["member", "credential", "retention"])
async def test_shared_admission_rechecks_current_authority_before_receipt(revocation: str) -> None:
    with shared_world() as lane:
        owner = lane.owners[0]
        lane.runtime.activate(lane.db, owner.ctx, owner.command())
        lane.db.commit()
        raw, headers = lane.notification(owner.cap.inbox_addresses[0])
        if revocation == "member":
            member = lane.db.scalar(
                select(Membership).where(
                    Membership.tenant_id == owner.ctx.tenant_id, Membership.user_id == owner.ctx.actor_id
                )
            )
            assert member is not None
            member.active = False
            member.version += 1
        elif revocation == "credential":
            connection = lane.db.get(ProviderConnection, owner.cap.connection_id)
            assert connection is not None
            connection.encrypted_credentials_ref = "independently-rotated-offline-credential"
            connection.version += 1
        else:
            policy = lane.db.get(RetentionPolicy, owner.cap.retention_policy_id)
            assert policy is not None
            policy.version += 1
        lane.db.commit()
        with pytest.raises(HTTPException) as refused:
            await lane.service.receive_shared(lane.endpoints[0], raw, headers)
        assert refused.value.status_code == 403
        lane.db.rollback()
        assert lane.db.scalar(select(func.count()).select_from(EmailIngressReceipt)) == 0
        assert lane.db.scalar(select(func.count()).select_from(EmailWebhookAdmission)) == 0


@pytest.mark.parametrize("mismatch", ["valid", "mime_recipient", "metadata_recipient", "message_id"])
async def test_fetched_normalized_and_original_recipient_evidence_cannot_cross_scope(mismatch: str) -> None:
    from aco.features.communications.shared_ingress import validate_message_recipients

    with shared_world() as lane:
        owner = lane.owners[0]
        active = lane.runtime.activate(lane.db, owner.ctx, owner.command())
        lane.db.commit()
        raw, headers = lane.notification(owner.cap.inbox_addresses[0])
        receipt = (await lane.service.receive_shared(lane.endpoints[0], raw, headers))[0]
        row = current_activation(lane.db, active.id, owner.ctx.tenant_id)
        # This read validator context is never passed to a worker or used as a mutation grant.
        ctx = EmailIngressContext(
            tenant_id=row.tenant_id,
            actor_id=row.worker_actor_id,
            correlation_id=uuid4(),
            activation_id=row.id,
            receipt_id=receipt.id,
            receipt_version=1,
            email_connection_id=row.email_connection_id,
            message_id="received:" + json.loads(raw)["data"]["email_id"],
            job_id=1,
            lease_token=uuid4(),
            storage_connection_id=row.storage_connection_id,
        )
        mime = MimeMessage()
        mime["From"] = "unknown@synthetic.invalid"
        mime["To"] = (
            "foreign@synthetic.invalid" if mismatch == "mime_recipient" else owner.cap.inbox_addresses[0]
        )
        mime["Message-ID"] = (
            "<different@synthetic.invalid>" if mismatch == "message_id" else "<exact@synthetic.invalid>"
        )
        mime.set_content("Explicit synthetic original only.")
        original = mime.as_bytes()
        message = EmailMessage(
            tenant_id=row.tenant_id,
            provider_connection_id=row.email_connection_id,
            provider_ref=ProviderReference(
                provider="resend", connection_id=row.email_connection_id, external_id=ctx.message_id
            ),
            native_identity="SIMULATED",
            synthetic=True,
            direction="incoming",
            kind="CUSTOMER_REPLY",
            received_at=now(),
            sender="unknown@synthetic.invalid",
            to=("foreign@synthetic.invalid",)
            if mismatch == "metadata_recipient"
            else owner.cap.inbox_addresses,
            subject="Synthetic only",
            text_body="Explicit synthetic original only.",
            message_id="<exact@synthetic.invalid>",
            source=EmailSource(
                kind="PROVIDER_ORIGINAL", content=original, integrity_sha256=sha256(original).hexdigest()
            ),
        )
        if mismatch == "valid":
            validate_message_recipients(lane.db, ctx, message, owner.cap.inbox_addresses)
        else:
            with pytest.raises(HTTPException) as refused:
                validate_message_recipients(lane.db, ctx, message, owner.cap.inbox_addresses)
            assert (
                refused.value.status_code == 403
                and refused.value.detail == "INGRESS_FETCHED_ENVELOPE_MISMATCH"
            )
        assert lane.db.scalar(select(func.count()).select_from(Communication)) == 0
