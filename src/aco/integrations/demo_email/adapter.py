"""Durable local synthetic native mailbox. Independent commits model a remote effect."""

import json
from collections.abc import Callable, Mapping
from datetime import timedelta
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aco.core.context import ActorContext, TenantContext
from aco.core.types import ProviderCapabilities, ProviderErrorCode, ProviderReference
from aco.features.identity.models import ProviderConnection, now
from aco.integrations.demo_email.models import (
    NativeEmailInvocation,
    NativeEmailOperation,
    NativeInboundMessage,
)
from aco.integrations.fake.email import FakeEmailAdapter
from aco.integrations.ports.email_context import EmailIngressContext, EmailWebhookContext
from aco.integrations.ports.email_dto import (
    DeliveryAttempt,
    EmailConnectionState,
    EmailMessage,
    EmailNotification,
    EmailResult,
    EmailSource,
    EmailSyncCursor,
    EmailSyncPage,
    OutboundEmail,
)


class DemoEmailAdapter(FakeEmailAdapter):
    """Reuse accepted validation/unsupported contracts, replacing native state with PostgreSQL."""

    provider = "demo_email"

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        tenant_id: UUID,
        connection_id: UUID,
        *,
        webhook_secret: str | None = None,
    ):
        self.webhook_secret = webhook_secret
        super().__init__(tenant_id=tenant_id, connection_id=connection_id)
        self.sessions = sessions

    async def _connection(self, db: AsyncSession, ctx: ActorContext) -> ProviderConnection:
        self._gate(ctx)
        row = await db.get(ProviderConnection, self.connection_id)
        if (
            row is None
            or row.tenant_id != self.tenant_id
            or row.provider != self.provider
            or row.provider_type != "email"
            or row.status != "READY"
        ):
            raise self._failure(ProviderErrorCode.AUTH_EXPIRED)
        return row

    async def publish_inbound(self, ctx: TenantContext, message: EmailMessage) -> None:
        """Explicit synthetic source input; not part of the mailbox invocation/worker port."""
        from fastapi import HTTPException

        from aco.features.identity.service import IdentityService, Permission

        if not isinstance(ctx, TenantContext):
            raise HTTPException(403, "HUMAN_REQUIRED")
        async with self.sessions.begin() as db:
            tenant = await db.run_sync(
                lambda store: IdentityService(store).authorize(ctx, Permission.SETTINGS)
            )
            await self._connection(db, ctx)
            self._scope(message.tenant_id, message.provider_connection_id)
            self._reference(message.provider_ref)
            if not tenant.is_demo or not message.synthetic or message.native_identity != "SIMULATED":
                raise HTTPException(403, "SYNTHETIC_INGRESS_ONLY")
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {
                    "key": (
                        f"demo-inbound:{ctx.tenant_id}:{self.connection_id}:"
                        f"{message.provider_ref.external_id}"
                    )
                },
            )
            prior = await db.scalar(
                select(NativeInboundMessage).where(
                    NativeInboundMessage.tenant_id == ctx.tenant_id,
                    NativeInboundMessage.provider_connection_id == self.connection_id,
                    NativeInboundMessage.native_message_id == message.provider_ref.external_id,
                )
            )
            digest = sha256(message.model_dump_json().encode()).hexdigest()
            if prior is not None:
                if prior.payload_sha256 != digest:
                    raise HTTPException(409, "NATIVE_INBOUND_IDENTITY_CONFLICT")
                return
            db.add(
                NativeInboundMessage(
                    tenant_id=ctx.tenant_id,
                    provider_connection_id=self.connection_id,
                    native_message_id=message.provider_ref.external_id,
                    message=message.model_dump(mode="json"),
                    payload_sha256=digest,
                )
            )

    async def publish_fixture(
        self, ctx: TenantContext, native_id: str, binding: str, build: Callable[[], EmailMessage]
    ) -> EmailMessage:
        """Atomic fixed-scenario source creation; replay returns the original bytes and time."""
        from fastapi import HTTPException

        from aco.features.identity.service import IdentityService, Permission

        if not isinstance(ctx, TenantContext):
            raise HTTPException(403, "HUMAN_REQUIRED")
        async with self.sessions.begin() as db:
            tenant = await db.run_sync(
                lambda store: IdentityService(store).authorize(ctx, Permission.SETTINGS)
            )
            await self._connection(db, ctx)
            if not tenant.is_demo:
                raise HTTPException(403, "SYNTHETIC_INGRESS_ONLY")
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": f"demo-inbound:{ctx.tenant_id}:{self.connection_id}:{native_id}"},
            )
            prior = await db.scalar(
                select(NativeInboundMessage).where(
                    NativeInboundMessage.tenant_id == ctx.tenant_id,
                    NativeInboundMessage.provider_connection_id == self.connection_id,
                    NativeInboundMessage.native_message_id == native_id,
                )
            )
            if prior is not None:
                message = EmailMessage.model_validate_json(json.dumps(prior.message))
                if dict(message.headers).get("x-aco-simulation-command") != binding:
                    raise HTTPException(409, "SIMULATION_REQUEST_CONFLICT")
                return message
            message = build()
            self._scope(message.tenant_id, message.provider_connection_id)
            self._reference(message.provider_ref)
            if (
                message.provider_ref.external_id != native_id
                or not message.synthetic
                or message.native_identity != "SIMULATED"
                or dict(message.headers).get("x-aco-simulation-command") != binding
            ):
                raise HTTPException(403, "SYNTHETIC_INGRESS_ONLY")
            db.add(
                NativeInboundMessage(
                    tenant_id=ctx.tenant_id,
                    provider_connection_id=self.connection_id,
                    native_message_id=native_id,
                    message=message.model_dump(mode="json"),
                    payload_sha256=sha256(message.model_dump_json().encode()).hexdigest(),
                )
            )
            return message

    def sign_fixture_notification(
        self, message: EmailMessage, event_id: str, secret: str
    ) -> tuple[bytes, dict[str, str]]:
        """Sign the fixed simulated native source; no signing SDK crosses the adapter boundary."""
        from svix.webhooks import Webhook

        self._scope(message.tenant_id, message.provider_connection_id)
        self._reference(message.provider_ref)
        if not message.synthetic or message.native_identity != "SIMULATED" or message.received_at is None:
            raise self._failure()
        event = EmailNotification(
            tenant_id=self.tenant_id,
            provider_connection_id=self.connection_id,
            provider_ref=ProviderReference(
                provider=self.provider, connection_id=self.connection_id, external_id=event_id
            ),
            message_ref=message.provider_ref,
            kind=message.kind,
            occurred_at=message.received_at,
            native_identity="SIMULATED",
            synthetic=True,
        )
        raw, at = event.model_dump_json().encode(), now()
        return raw, {
            "svix-id": event_id,
            "svix-timestamp": str(int(at.timestamp())),
            "svix-signature": Webhook(secret).sign(event_id, at, raw.decode()),
        }

    async def validate_inbound_source(self, ctx: TenantContext) -> EmailResult[EmailConnectionState]:
        """Validate actual local source tables and exact configured synthetic connection before READY."""
        from fastapi import HTTPException

        from aco.features.identity.service import IdentityService, Permission

        if not isinstance(ctx, TenantContext):
            raise HTTPException(403, "HUMAN_REQUIRED")
        async with self.sessions.begin() as db:
            tenant = await db.run_sync(
                lambda store: IdentityService(store).authorize(ctx, Permission.SETTINGS)
            )
            row = await db.get(ProviderConnection, self.connection_id)
            if (
                not tenant.is_demo
                or row is None
                or row.tenant_id != ctx.tenant_id
                or row.provider != self.provider
                or row.provider_type != "email"
                or row.status not in {"NOT_CONFIGURED", "READY"}
            ):
                raise HTTPException(403, "SYNTHETIC_SOURCE_CONNECTION_REQUIRED")
            # Exercise the durable source tables without inventing a customer message.
            await db.execute(
                select(NativeInboundMessage.id)
                .where(
                    NativeInboundMessage.tenant_id == ctx.tenant_id,
                    NativeInboundMessage.provider_connection_id == self.connection_id,
                )
                .limit(1)
            )
            await db.execute(
                select(NativeEmailOperation.id)
                .where(
                    NativeEmailOperation.tenant_id == ctx.tenant_id,
                    NativeEmailOperation.provider_connection_id == self.connection_id,
                )
                .limit(1)
            )
        return EmailResult[EmailConnectionState](
            **self._observation(ctx), value=EmailConnectionState(connected=True, mode="SIMULATED")
        )

    async def describe_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            features=frozenset(
                {
                    "send_email",
                    "get_message",
                    "verify_webhook",
                    "fetch_thread",
                    "sync_messages",
                    "validate_connection",
                    "disconnect",
                    "synthetic",
                    "durable_native_ledger",
                    "vendor_key_ttl",
                }
            )
        )

    async def validate_connection(self, ctx: ActorContext) -> EmailResult[EmailConnectionState]:
        async with self.sessions() as db:
            await self._connection(db, ctx)
        return EmailResult[EmailConnectionState](
            **self._observation(ctx), value=EmailConnectionState(connected=True, mode="SIMULATED")
        )

    async def send_email(self, ctx: ActorContext, email: OutboundEmail) -> DeliveryAttempt:
        self._gate(ctx)
        self._scope(email.tenant_id, email.provider_connection_id)
        self._content_references(email)
        fingerprint = sha256(email.model_dump_json().encode()).hexdigest()
        async with self.sessions.begin() as db:
            await db.execute(text("SET LOCAL lock_timeout = '2s'"))
            await self._connection(db, ctx)
            # Connection serialization is independent of caller-held action/tenant authority rows.
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": str(email.operation_id)},
            )
            prior = await db.scalar(
                select(NativeEmailOperation)
                .where(
                    NativeEmailOperation.tenant_id == ctx.tenant_id,
                    NativeEmailOperation.provider_connection_id == self.connection_id,
                    NativeEmailOperation.operation_id == email.operation_id,
                )
                .order_by(NativeEmailOperation.generation.desc())
                .limit(1)
            )
            request_id = f"simulated-request:{uuid4()}"
            if prior and prior.vendor_key_expires_at > now():
                if prior.payload_hash != fingerprint:
                    raise self._failure()
                saved = EmailMessage.model_validate_json(json.dumps(prior.message))
                generation = prior.generation
            else:
                generation = prior.generation + 1 if prior else 1
                message_ref = ProviderReference(
                    provider=self.provider,
                    connection_id=self.connection_id,
                    external_id=f"simulated-message:{uuid4()}",
                )
                values = email.model_dump(exclude={"tenant_id", "provider_connection_id"})
                values["provider_thread_ref"] = email.provider_thread_ref or ProviderReference(
                    provider=self.provider,
                    connection_id=self.connection_id,
                    external_id="simulated-thread:" + str(email.conversation_thread_id),
                )
                saved = EmailMessage(
                    **values,
                    tenant_id=ctx.tenant_id,
                    provider_connection_id=self.connection_id,
                    provider_ref=message_ref,
                    native_identity="SIMULATED",
                    synthetic=True,
                    direction="outgoing",
                    kind="OUTBOUND_ECHO",
                    sent_at=now(),
                    source=EmailSource(kind="UNAVAILABLE"),
                )
                db.add(
                    NativeEmailOperation(
                        tenant_id=ctx.tenant_id,
                        provider_connection_id=self.connection_id,
                        operation_id=email.operation_id,
                        generation=generation,
                        payload_hash=fingerprint,
                        native_message_id=message_ref.external_id,
                        native_request_id=request_id,
                        vendor_key_expires_at=now() + timedelta(hours=24),
                        message=saved.model_dump(mode="json"),
                    )
                )
            db.add(
                NativeEmailInvocation(
                    tenant_id=ctx.tenant_id,
                    provider_connection_id=self.connection_id,
                    operation_id=email.operation_id,
                    method="send_email",
                    payload_hash=fingerprint,
                    native_request_id=request_id,
                    result="ACCEPTED",
                )
            )
        observation = self._observation(ctx)
        observation["provider_ref"] = ProviderReference(
            provider=self.provider, connection_id=self.connection_id, external_id=request_id
        )
        return DeliveryAttempt(
            **observation, operation_id=email.operation_id, status="ACCEPTED", message_ref=saved.provider_ref
        )

    async def get_message(
        self, ctx: ActorContext | EmailIngressContext, message_ref: ProviderReference
    ) -> EmailResult[EmailMessage]:
        self._reference(message_ref)
        async with self.sessions() as db:
            if isinstance(ctx, EmailIngressContext):
                from aco.features.communications.ingress import current_receipt

                await db.run_sync(lambda store: current_receipt(store, ctx))
                if ctx.email_connection_id != self.connection_id or ctx.message_id != message_ref.external_id:
                    raise self._failure()
            else:
                await self._connection(db, ctx)
            inbound = await db.scalar(
                select(NativeInboundMessage).where(
                    NativeInboundMessage.tenant_id == ctx.tenant_id,
                    NativeInboundMessage.provider_connection_id == self.connection_id,
                    NativeInboundMessage.native_message_id == message_ref.external_id,
                )
            )
            if inbound is not None:
                value = EmailMessage.model_validate_json(json.dumps(inbound.message))
            else:
                row = await db.scalar(
                    select(NativeEmailOperation).where(
                        NativeEmailOperation.tenant_id == ctx.tenant_id,
                        NativeEmailOperation.provider_connection_id == self.connection_id,
                        NativeEmailOperation.native_message_id == message_ref.external_id,
                    )
                )
                if row is None:
                    raise self._failure()
                value = EmailMessage.model_validate_json(json.dumps(row.message))
        return EmailResult[EmailMessage](**self._observation(ctx), value=value)

    async def verify_webhook(
        self, ctx: ActorContext | EmailWebhookContext, raw_body: bytes, headers: Mapping[str, str]
    ) -> EmailResult[tuple[EmailNotification, ...]]:
        from fastapi import HTTPException
        from svix.webhooks import Webhook, WebhookVerificationError

        from aco.features.communications.ingress import validate_webhook

        if not isinstance(ctx, EmailWebhookContext) or not self.webhook_secret:
            raise self._failure(ProviderErrorCode.UNSUPPORTED_CAPABILITY)
        async with self.sessions() as db:
            await db.run_sync(lambda store: validate_webhook(store, ctx, raw_body, self.webhook_secret or ""))
        try:
            Webhook(self.webhook_secret).verify(raw_body, dict(headers))
        except WebhookVerificationError as error:
            raise HTTPException(403, "INGRESS_SIGNATURE_INVALID") from error
        # No tenant supplied by callback is accepted as routing authority.
        event = EmailNotification.model_validate_json(raw_body)
        if (
            event.tenant_id != ctx.tenant_id
            or event.provider_connection_id != self.connection_id
            or event.provider_ref.provider != self.provider
            or event.native_identity != "SIMULATED"
            or not event.synthetic
        ):
            raise HTTPException(403, "INGRESS_NOTIFICATION_PROVENANCE")
        return EmailResult[tuple[EmailNotification, ...]](**self._observation(ctx), value=(event,))

    async def fetch_thread(
        self, ctx: ActorContext, thread_ref: ProviderReference
    ) -> EmailResult[tuple[EmailMessage, ...]]:
        self._reference(thread_ref)
        page = await self.sync_messages(ctx)
        messages = tuple(m for m in page.value.messages if m.provider_thread_ref == thread_ref)
        return EmailResult[tuple[EmailMessage, ...]](**self._observation(ctx), value=messages)

    async def sync_messages(
        self, ctx: ActorContext, cursor: EmailSyncCursor | None = None
    ) -> EmailResult[EmailSyncPage]:
        offset = 0
        if cursor:
            self._scope(cursor.tenant_id, cursor.provider_connection_id)
            try:
                offset = int(cursor.token)
                if offset < 0:
                    raise ValueError
            except ValueError:
                raise self._failure() from None
        async with self.sessions() as db:
            await self._connection(db, ctx)
            rows = list(
                await db.scalars(
                    select(NativeEmailOperation)
                    .where(
                        NativeEmailOperation.tenant_id == ctx.tenant_id,
                        NativeEmailOperation.provider_connection_id == self.connection_id,
                    )
                    .order_by(NativeEmailOperation.created_at, NativeEmailOperation.id)
                )
            )
        return EmailResult[EmailSyncPage](
            **self._observation(ctx),
            value=EmailSyncPage(
                messages=tuple(
                    EmailMessage.model_validate_json(json.dumps(row.message)) for row in rows[offset:]
                ),
                notifications=(),
                next_cursor=EmailSyncCursor(
                    tenant_id=ctx.tenant_id, provider_connection_id=self.connection_id, token=str(len(rows))
                ),
            ),
        )

    async def disconnect(self, ctx: ActorContext) -> EmailResult[EmailConnectionState]:
        async with self.sessions.begin() as db:
            row = await self._connection(db, ctx)
            row.status = "DISCONNECTED"
            row.version += 1
        return EmailResult[EmailConnectionState](
            **self._observation(ctx), value=EmailConnectionState(connected=False, mode="SIMULATED")
        )


class DemoEmailRuntime:
    provider = "demo_email"

    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self.sessions = sessions

    def bind(self, tenant_id: UUID, connection_id: UUID) -> DemoEmailAdapter:
        return DemoEmailAdapter(self.sessions, tenant_id, connection_id)
