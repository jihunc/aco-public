"""In-memory synthetic email provider. No sockets, remote mailbox or durable effect claim.

The constructor is a trusted composition/test seam, never an authentication grant.
Idempotency survives only this object's lifetime; T10's durable dispatch ledger is
responsible for crash/transaction recovery. Artifact UUIDs remain opaque: this
adapter does not fetch them or establish storage authorization.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from aco.core.context import ActorContext, ServiceContext, TenantContext
from aco.core.types import (
    CostQuality,
    ProviderCapabilities,
    ProviderCost,
    ProviderErrorCode,
    ProviderFailure,
    ProviderReference,
)
from aco.integrations.ports.email import EmailFailure
from aco.integrations.ports.email_context import EmailIngressContext, EmailWebhookContext
from aco.integrations.ports.email_dto import (
    DeliveryAttempt,
    EmailConnectionState,
    EmailMessage,
    EmailNotification,
    EmailObjectReference,
    EmailResult,
    EmailSource,
    EmailSubscription,
    EmailSyncCursor,
    EmailSyncPage,
    InboundEmail,
    MailboxAuthorization,
    NotificationSubscriptionRequest,
    OutboundEmail,
)


class FakeEmailAdapter:
    provider = "fake_email"

    def __init__(
        self,
        *,
        tenant_id: UUID,
        connection_id: UUID,
        messages: tuple[InboundEmail, ...] = (),
        notifications: tuple[EmailNotification, ...] = (),
        send_failure: ProviderFailure | None = None,
    ):
        self.tenant_id = tenant_id
        self.connection_id = connection_id
        self._connected = True
        self._send_failure = send_failure
        self._messages: dict[str, EmailMessage] = {}
        self._notifications: dict[str, EmailNotification] = {}
        self._operations: dict[UUID, tuple[str, DeliveryAttempt | ProviderFailure]] = {}
        self._cursors: dict[str, tuple[int, int]] = {}
        for message in messages:
            self._scope(message.tenant_id, message.provider_connection_id)
            self._reference(message.provider_ref)
            self._content_references(message)
            if not message.synthetic or message.native_identity != "SIMULATED":
                raise self._failure()
            prior = self._messages.get(message.provider_ref.external_id)
            if prior is not None and prior != message:
                raise self._failure()
            self._messages[message.provider_ref.external_id] = message
        for event in notifications:
            self._scope(event.tenant_id, event.provider_connection_id)
            self._reference(event.provider_ref)
            self._reference(event.message_ref)
            if not event.synthetic or event.native_identity != "SIMULATED":
                raise self._failure()
            prior_event = self._notifications.get(event.provider_ref.external_id)
            if prior_event is not None and prior_event != event:
                raise self._failure()
            self._notifications[event.provider_ref.external_id] = event

    def _failure(self, code: ProviderErrorCode = ProviderErrorCode.VALIDATION_FAILED) -> EmailFailure:
        return EmailFailure(
            ProviderFailure(
                code=code,
                retryable=False,
                may_have_side_effect=False,
                request_id=f"simulated-request:{uuid4()}",
            )
        )

    def _scope(self, tenant_id: UUID, connection_id: UUID) -> None:
        if (tenant_id, connection_id) != (self.tenant_id, self.connection_id):
            raise self._failure()

    def _gate(
        self, ctx: ActorContext | EmailIngressContext | EmailWebhookContext, *, require_connected: bool = True
    ) -> None:
        if not isinstance(ctx, TenantContext | ServiceContext):
            raise self._failure()
        self._scope(ctx.tenant_id, self.connection_id)
        if require_connected and not self._connected:
            raise self._failure(ProviderErrorCode.AUTH_EXPIRED)

    def _reference(self, reference: ProviderReference) -> None:
        if (reference.provider, reference.connection_id) != (self.provider, self.connection_id):
            raise self._failure()

    def _object_reference(self, reference: EmailObjectReference | None) -> None:
        if reference is not None and reference.provider_ref is not None:
            self._reference(reference.provider_ref)

    def _content_references(self, email: EmailMessage | OutboundEmail) -> None:
        if email.provider_thread_ref is not None:
            self._reference(email.provider_thread_ref)
        for attachment in email.attachments:
            self._object_reference(attachment.reference)
        if isinstance(email, EmailMessage):
            self._object_reference(email.source.reference)

    def _observation(self, ctx: ActorContext | EmailIngressContext | EmailWebhookContext) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "provider_connection_id": self.connection_id,
            "provider_ref": ProviderReference(
                provider=self.provider,
                connection_id=self.connection_id,
                external_id=f"simulated-request:{uuid4()}",
            ),
            "correlation_id": ctx.correlation_id,
            "observed_at": datetime.now(UTC),
            "revision": "simulated:1",
            "native_identity": "SIMULATED",
            "synthetic": True,
            "authenticity": "SIMULATED",
            "attempt": 1,
            "cost": ProviderCost(amount=None, currency="AUD", quality=CostQuality.UNKNOWN),
        }

    async def describe_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            features=frozenset(
                {
                    "send_email",
                    "get_message",
                    "fetch_thread",
                    "sync_messages",
                    "validate_connection",
                    "disconnect",
                    "synthetic",
                    "process_lifetime_idempotency",
                }
            )
        )

    async def connect_authorized_mailbox(
        self, ctx: ActorContext, authorization: MailboxAuthorization
    ) -> EmailResult[EmailConnectionState]:
        self._gate(ctx, require_connected=False)
        raise self._failure(ProviderErrorCode.UNSUPPORTED_CAPABILITY)

    async def validate_connection(self, ctx: ActorContext) -> EmailResult[EmailConnectionState]:
        self._gate(ctx, require_connected=False)
        return EmailResult[EmailConnectionState](
            **self._observation(ctx), value=EmailConnectionState(connected=self._connected, mode="SIMULATED")
        )

    async def send_email(self, ctx: ActorContext, email: OutboundEmail) -> DeliveryAttempt:
        self._gate(ctx)
        self._scope(email.tenant_id, email.provider_connection_id)
        self._content_references(email)
        fingerprint = sha256(email.model_dump_json().encode()).hexdigest()
        prior = self._operations.get(email.operation_id)
        if prior is not None:
            if prior[0] != fingerprint:
                raise self._failure()
            if isinstance(prior[1], ProviderFailure):
                raise EmailFailure(prior[1])
            return prior[1]
        if self._send_failure is not None and not self._send_failure.may_have_side_effect:
            self._operations[email.operation_id] = (fingerprint, self._send_failure)
            raise EmailFailure(self._send_failure)

        message_ref = ProviderReference(
            provider=self.provider,
            connection_id=self.connection_id,
            external_id=f"simulated-message:{uuid4()}",
        )
        thread_ref = email.provider_thread_ref or ProviderReference(
            provider=self.provider,
            connection_id=self.connection_id,
            external_id=f"simulated-thread:{email.conversation_thread_id}",
        )
        message_id = email.message_id or f"<simulated-{uuid4()}@aco.example.test>"
        # Normalized capture only: not raw MIME, provider-original or an archive-ready
        # T09 artifact. The full consumer must compose/fetch genuine permitted bytes.
        # Opaque references survive exactly; unavailable attachment bytes are not invented.
        composed = (
            email.model_copy(
                update={
                    "message_id": message_id,
                    "provider_thread_ref": thread_ref,
                }
            )
            .model_dump_json()
            .encode()
        )
        headers = [
            ("from", email.sender),
            ("to", ", ".join(email.to)),
            ("subject", email.subject),
            ("message-id", message_id),
        ]
        if email.cc:
            headers.append(("cc", ", ".join(email.cc)))
        if email.in_reply_to:
            headers.append(("in-reply-to", email.in_reply_to))
        if email.references:
            headers.append(("references", " ".join(email.references)))
        content = email.model_dump(
            exclude={
                "tenant_id",
                "provider_connection_id",
                "origin_invoice_id",
                "conversation_thread_id",
                "operation_id",
                "provider_thread_ref",
                "message_id",
            }
        )
        saved = EmailMessage(
            **content,
            tenant_id=self.tenant_id,
            provider_connection_id=self.connection_id,
            origin_invoice_id=email.origin_invoice_id,
            conversation_thread_id=email.conversation_thread_id,
            operation_id=email.operation_id,
            provider_ref=message_ref,
            provider_thread_ref=thread_ref,
            message_id=message_id,
            native_identity="SIMULATED",
            synthetic=True,
            authenticity="SIMULATED",
            revision="simulated:1",
            direction="outgoing",
            kind="OUTBOUND_ECHO",
            sent_at=datetime.now(UTC),
            headers=tuple(headers),
            source=EmailSource(
                kind="ACO_COMPOSED",
                content=composed,
                integrity_sha256=sha256(composed).hexdigest(),
                content_type="application/vnd.aco.outbound-email+json",
            ),
        )
        self._messages[message_ref.external_id] = saved
        if self._send_failure is not None:
            self._operations[email.operation_id] = (fingerprint, self._send_failure)
            raise EmailFailure(self._send_failure)
        attempt = DeliveryAttempt(
            **self._observation(ctx),
            operation_id=email.operation_id,
            status="ACCEPTED",
            message_ref=message_ref,
        )
        self._operations[email.operation_id] = (fingerprint, attempt)
        return attempt

    async def get_message(
        self, ctx: ActorContext | EmailIngressContext, message_ref: ProviderReference
    ) -> EmailResult[EmailMessage]:
        self._gate(ctx)
        self._reference(message_ref)
        message = self._messages.get(message_ref.external_id)
        if message is None:
            raise self._failure()
        return EmailResult[EmailMessage](**self._observation(ctx), value=message)

    async def fetch_thread(
        self, ctx: ActorContext, thread_ref: ProviderReference
    ) -> EmailResult[tuple[EmailMessage, ...]]:
        self._gate(ctx)
        self._reference(thread_ref)
        messages = tuple(item for item in self._messages.values() if item.provider_thread_ref == thread_ref)
        if not messages:
            raise self._failure()
        return EmailResult[tuple[EmailMessage, ...]](**self._observation(ctx), value=messages)

    async def sync_messages(
        self, ctx: ActorContext, cursor: EmailSyncCursor | None = None
    ) -> EmailResult[EmailSyncPage]:
        self._gate(ctx)
        start = (0, 0)
        if cursor is not None:
            self._scope(cursor.tenant_id, cursor.provider_connection_id)
            if cursor.token not in self._cursors:
                raise self._failure()
            start = self._cursors[cursor.token]
        token = f"simulated-cursor:{uuid4()}"
        self._cursors[token] = (len(self._messages), len(self._notifications))
        return EmailResult[EmailSyncPage](
            **self._observation(ctx),
            value=EmailSyncPage(
                messages=tuple(self._messages.values())[start[0] :],
                notifications=tuple(self._notifications.values())[start[1] :],
                next_cursor=EmailSyncCursor(
                    tenant_id=self.tenant_id, provider_connection_id=self.connection_id, token=token
                ),
            ),
        )

    async def subscribe_or_renew_notifications(
        self, ctx: ActorContext, request: NotificationSubscriptionRequest
    ) -> EmailResult[EmailSubscription]:
        self._gate(ctx)
        if request.subscription_ref is not None:
            self._reference(request.subscription_ref)
        raise self._failure(ProviderErrorCode.UNSUPPORTED_CAPABILITY)

    async def disconnect(self, ctx: ActorContext) -> EmailResult[EmailConnectionState]:
        self._gate(ctx, require_connected=False)
        self._connected = False
        return EmailResult[EmailConnectionState](
            **self._observation(ctx), value=EmailConnectionState(connected=False, mode="SIMULATED")
        )

    async def verify_webhook(
        self, ctx: ActorContext | EmailWebhookContext, raw_body: bytes, headers: Mapping[str, str]
    ) -> EmailResult[tuple[EmailNotification, ...]]:
        self._gate(ctx)
        raise self._failure(ProviderErrorCode.UNSUPPORTED_CAPABILITY)
