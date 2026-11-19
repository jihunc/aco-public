"""A fixed account webhook verifier; raw signed metadata precedes tenant lookup.

No API key or HTTP client is needed to verify a receiving event. This adapter
cannot select a tenant, fetch a message or grant capture/AI/outbound authority.
"""

import json
import re
from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256
from uuid import UUID

from pydantic import SecretStr, ValidationError
from svix.webhooks import Webhook, WebhookVerificationError

from aco.core.types import ProviderErrorCode, ProviderFailure
from aco.integrations.ports.email import EmailFailure
from aco.integrations.ports.email_dto import mailbox
from aco.integrations.ports.email_routing import VerifiedEmailRoute


class ResendWebhookRouter:
    def __init__(self, *, endpoint_id: UUID, account_id: str, secret: SecretStr):
        if not account_id or len(account_id) > 200:
            raise ValueError("A fixed server account identity is required")
        self.provider = "resend"
        self.endpoint_id, self.account_id = endpoint_id, account_id
        self.secret = secret
        self.secret_sha256 = sha256(secret.get_secret_value().encode()).hexdigest()

    async def verify_route(self, raw: bytes, headers: Mapping[str, str]) -> VerifiedEmailRoute:
        try:
            if len(raw) > 65536:
                raise ValueError()
            normalized = {key.lower(): value for key, value in headers.items()}
            Webhook(self.secret.get_secret_value()).verify(raw, normalized)
            payload = json.loads(raw)
            event_id = normalized["svix-id"]
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", event_id):
                raise ValueError()
            if payload["type"] != "email.received":
                raise ValueError()
            at = datetime.fromisoformat(payload["created_at"].replace("Z", "+00:00"))
            if at.tzinfo is None:
                raise ValueError()
            data = payload["data"]
            native = UUID(data["email_id"])
            recipients: set[str] = set()
            for field in ("to", "received_for"):
                values = data.get(field, [])
                if not isinstance(values, list) or len(values) > 50:
                    raise ValueError()
                recipients.update(mailbox(value).casefold() for value in values)
            return VerifiedEmailRoute(
                event_id=event_id,
                message_id=f"received:{native}",
                recipients=tuple(sorted(recipients)),
                sender=mailbox(data["from"]).casefold() if data.get("from") else None,
                raw_sha256=sha256(raw).hexdigest(),
            )
        except (WebhookVerificationError, KeyError, TypeError, ValueError, ValidationError):
            raise EmailFailure(
                ProviderFailure(
                    code=ProviderErrorCode.VALIDATION_FAILED,
                    retryable=False,
                    may_have_side_effect=False,
                )
            ) from None
