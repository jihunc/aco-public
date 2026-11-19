"""Asynchronous email boundary. Context values alone do not establish authority."""

from collections.abc import Mapping
from typing import Protocol, runtime_checkable
from uuid import UUID

from aco.core.context import ActorContext
from aco.core.types import ProviderCapabilities, ProviderFailure, ProviderReference
from aco.integrations.ports.email_context import EmailIngressContext, EmailWebhookContext
from aco.integrations.ports.email_dto import (
    DeliveryAttempt,
    EmailConnectionState,
    EmailMessage,
    EmailNotification,
    EmailResult,
    EmailSubscription,
    EmailSyncCursor,
    EmailSyncPage,
    MailboxAuthorization,
    NotificationSubscriptionRequest,
    OutboundEmail,
)


class EmailFailure(Exception):
    def __init__(self, failure: ProviderFailure):
        self.failure = failure
        super().__init__(failure.code.value)


@runtime_checkable
class EmailPort(Protocol):
    connection_id: UUID

    async def connect_authorized_mailbox(
        self, ctx: ActorContext, authorization: MailboxAuthorization
    ) -> EmailResult[EmailConnectionState]: ...
    async def validate_connection(self, ctx: ActorContext) -> EmailResult[EmailConnectionState]: ...
    async def send_email(self, ctx: ActorContext, email: OutboundEmail) -> DeliveryAttempt: ...
    async def get_message(
        self, ctx: ActorContext | EmailIngressContext, message_ref: ProviderReference
    ) -> EmailResult[EmailMessage]: ...
    async def fetch_thread(
        self, ctx: ActorContext, thread_ref: ProviderReference
    ) -> EmailResult[tuple[EmailMessage, ...]]: ...
    async def sync_messages(
        self, ctx: ActorContext, cursor: EmailSyncCursor | None = None
    ) -> EmailResult[EmailSyncPage]: ...
    async def subscribe_or_renew_notifications(
        self, ctx: ActorContext, request: NotificationSubscriptionRequest
    ) -> EmailResult[EmailSubscription]: ...
    async def disconnect(self, ctx: ActorContext) -> EmailResult[EmailConnectionState]: ...
    async def verify_webhook(
        self, ctx: ActorContext | EmailWebhookContext, raw_body: bytes, headers: Mapping[str, str]
    ) -> EmailResult[tuple[EmailNotification, ...]]: ...
    async def describe_capabilities(self) -> ProviderCapabilities: ...
