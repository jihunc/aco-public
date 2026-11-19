"""Real Svix raw-byte verification before shared-account recipient routing; no HTTP."""

import base64
import json
from uuid import uuid4

import pytest
from pydantic import SecretStr
from svix.webhooks import Webhook

from aco.features.identity.models import now
from aco.integrations.ports.email import EmailFailure


async def test_shared_resend_route_uses_signed_recipients_without_aco_activation_identity() -> None:
    from aco.integrations.resend.routing import ResendWebhookRouter

    endpoint = uuid4()
    secret = "whsec_" + base64.b64encode(b"offline-shared-account-webhook-key").decode()
    router = ResendWebhookRouter(
        endpoint_id=endpoint, account_id="shared-resend-account", secret=SecretStr(secret)
    )
    at = now()
    native = uuid4()
    raw = json.dumps(
        {
            "type": "email.received",
            "created_at": at.isoformat(),
            "data": {
                "email_id": str(native),
                "to": ["company-a@aco.example.com"],
                "activation_id": str(uuid4()),
                "tenant_id": str(uuid4()),
            },
        }
    ).encode()
    event = "msg_shared_fixture"
    headers = {
        "svix-id": event,
        "svix-timestamp": str(int(at.timestamp())),
        "svix-signature": Webhook(secret).sign(event, at, raw.decode()),
    }
    route = await router.verify_route(raw, headers)
    assert route.event_id == event
    assert route.message_id == f"received:{native}"
    assert route.recipients == ("company-a@aco.example.com",)
    assert router.endpoint_id == endpoint and router.account_id == "shared-resend-account"
    with pytest.raises(EmailFailure):
        await router.verify_route(raw + b" ", headers)


async def test_shared_resend_route_preserves_signed_delivery_recipients_for_fail_closed_resolution() -> None:
    from aco.integrations.resend.routing import ResendWebhookRouter

    secret = "whsec_" + base64.b64encode(b"offline-shared-account-webhook-key").decode()
    router = ResendWebhookRouter(
        endpoint_id=uuid4(), account_id="shared-resend-account", secret=SecretStr(secret)
    )
    at = now()
    raw = json.dumps(
        {
            "type": "email.received",
            "created_at": at.isoformat(),
            "data": {
                "email_id": str(uuid4()),
                "to": ["company-a@aco.example.com", "company-b@aco.example.com"],
                "cc": ["finance@example.test"],
                "received_for": ["company-a@aco.example.com"],
            },
        }
    ).encode()
    event = "msg_shared_mixed_fixture"
    headers = {
        "svix-id": event,
        "svix-timestamp": str(int(at.timestamp())),
        "svix-signature": Webhook(secret).sign(event, at, raw.decode()),
    }
    assert (await router.verify_route(raw, headers)).recipients == (
        "company-a@aco.example.com",
        "company-b@aco.example.com",
    )
