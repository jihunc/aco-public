"""Svix verifies exact bytes; provider delivery is distinct from sender authority or DB admission."""

import json
import re
from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256
from uuid import UUID

from pydantic import ValidationError
from svix.webhooks import Webhook, WebhookVerificationError

from aco.core.types import ProviderErrorCode, ProviderFailure, ProviderReference
from aco.integrations.ports.email import EmailFailure
from aco.integrations.ports.email_context import EmailWebhookContext
from aco.integrations.ports.email_dto import EmailNotification, mailbox
from aco.integrations.resend.settings import ResendBinding


def verified_notifications(
    binding: ResendBinding, ctx: EmailWebhookContext, raw: bytes, headers: Mapping[str, str]
) -> tuple[EmailNotification, ...]:
    def invalid() -> EmailFailure:
        return EmailFailure(
            ProviderFailure(
                code=ProviderErrorCode.VALIDATION_FAILED, retryable=False, may_have_side_effect=False
            )
        )

    if not binding.webhook_secret or len(raw) > 65536 or sha256(raw).hexdigest() != ctx.raw_sha256:
        raise invalid()
    normalized = {name.lower(): value for name, value in headers.items()}
    try:
        Webhook(binding.webhook_secret.get_secret_value()).verify(raw, normalized)
        payload = json.loads(raw)
        event_id = normalized["svix-id"]
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", event_id):
            raise ValueError()
        event_type = payload["type"]
        data = payload["data"]
        native = UUID(data["email_id"])
        mapping = {
            "email.received": ("received", "CUSTOMER_REPLY"),
            "email.sent": ("sent", "OUTBOUND_ECHO"),
            "email.delivered": ("sent", "DELIVERY_RECEIPT"),
            "email.bounced": ("sent", "BOUNCE"),
            "email.failed": ("sent", "BOUNCE"),
        }
        if event_type not in mapping:
            raise EmailFailure(
                ProviderFailure(
                    code=ProviderErrorCode.UNSUPPORTED_CAPABILITY, retryable=False, may_have_side_effect=False
                )
            )
        direction, kind = mapping[event_type]
        if direction == "received":
            recipients: set[str] = set()
            for key in ("to", "received_for"):
                values = data.get(key, [])
                if not isinstance(values, list) or len(values) > 50:
                    raise ValueError()
                recipients.update(mailbox(value).casefold() for value in values)
            if not recipients or not recipients.issubset(
                {mailbox(item).casefold() for item in binding.inbox_addresses}
            ):
                raise ValueError()
        return (
            EmailNotification.model_validate(
                dict(
                    tenant_id=binding.tenant_id,
                    provider_connection_id=binding.connection_id,
                    provider_ref=ProviderReference(
                        provider="resend", connection_id=binding.connection_id, external_id=event_id
                    ),
                    message_ref=ProviderReference(
                        provider="resend",
                        connection_id=binding.connection_id,
                        external_id=f"{direction}:{native}",
                    ),
                    kind=kind,
                    occurred_at=datetime.fromisoformat(payload["created_at"].replace("Z", "+00:00")),
                    native_identity="SIMULATED" if binding.evidence_mode == "MOCKED_HTTP" else "PROVIDER",
                    synthetic=binding.evidence_mode == "MOCKED_HTTP",
                )
            ),
        )
    except (WebhookVerificationError, KeyError, TypeError, ValueError, ValidationError):
        raise invalid() from None
