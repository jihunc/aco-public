"""Real synthetic source + Svix notification + Procrastinate worker + private APP storage."""

import asyncio
import base64
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from email.message import EmailMessage as MimeMessage
from hashlib import sha256
from typing import cast
from uuid import UUID, uuid4

from action_support import World, action_world
from artifact_support import storage_configuration
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import select
from svix.webhooks import Webhook

from aco.core.settings import Settings
from aco.core.types import ProviderReference
from aco.features.accounting.models import CustomerContact, InvoiceSnapshot
from aco.features.artifacts.service import ArtifactService
from aco.features.communications.inbound import AgentProducer
from aco.features.communications.ingress import EmailIngressService, dispatch_email_ingress
from aco.features.communications.ingress_dto import ActivateIngress
from aco.features.identity.models import ProviderConnection, now
from aco.integrations.demo_email.adapter import DemoEmailAdapter
from aco.integrations.ports.email_dto import EmailMessage, EmailNotification, EmailSource
from aco.integrations.s3.adapter import StorageRuntime
from aco.worker.app import WorkerRuntime


@dataclass
class IngressWorld:
    world: World
    email_id: UUID
    storage_id: UUID
    activation_id: UUID
    secret: str
    service: EmailIngressService

    def message(
        self,
        *,
        native_id: str | None = None,
        thread: str | None = None,
        body: str = "Synthetic incoming fixture references INV-101.",
        kind: str = "CUSTOMER_REPLY",
    ) -> EmailMessage:
        invoice = self.world.store.get(InvoiceSnapshot, self.world.invoice_id)
        assert invoice is not None
        contact = self.world.store.scalar(
            select(CustomerContact).where(CustomerContact.customer_id == invoice.customer_id)
        )
        assert contact is not None and contact.email is not None
        key = native_id or f"simulated-inbound:{uuid4()}"
        mime = MimeMessage()
        mime["From"], mime["To"], mime["Subject"] = (
            contact.email,
            "collections@synthetic.invalid",
            "Explicit synthetic reply",
        )
        mime["Message-ID"] = f"<{uuid4()}@synthetic.invalid>"
        mime.set_content(body)
        raw = mime.as_bytes()
        return EmailMessage.model_validate(
            dict(
                tenant_id=self.world.ctx.tenant_id,
                provider_connection_id=self.email_id,
                provider_ref=ProviderReference(
                    provider="demo_email", connection_id=self.email_id, external_id=key
                ),
                native_identity="SIMULATED",
                synthetic=True,
                authenticity="SIMULATED",
                direction="incoming",
                kind=kind,
                received_at=now(),
                sender=contact.email,
                to=("collections@synthetic.invalid",),
                subject="Explicit synthetic reply",
                text_body=body,
                message_id=str(mime["Message-ID"]),
                headers=tuple(mime.items()),
                provider_thread_ref=ProviderReference(
                    provider="demo_email",
                    connection_id=self.email_id,
                    external_id=thread or f"simulated-thread:{uuid4()}",
                ),
                source=EmailSource(
                    kind="PROVIDER_ORIGINAL", content=raw, integrity_sha256=sha256(raw).hexdigest()
                ),
            )
        )

    def notification(
        self, message: EmailMessage, *, event_id: str | None = None
    ) -> tuple[bytes, dict[str, str]]:
        key = event_id or f"simulated-event:{uuid4()}"
        event = EmailNotification(
            tenant_id=self.world.ctx.tenant_id,
            provider_connection_id=self.email_id,
            provider_ref=ProviderReference(
                provider="demo_email", connection_id=self.email_id, external_id=key
            ),
            message_ref=message.provider_ref,
            kind=message.kind,
            occurred_at=now(),
            native_identity="SIMULATED",
            synthetic=True,
        )
        raw = event.model_dump_json().encode()
        at = now()
        return raw, {
            "svix-id": key,
            "svix-timestamp": str(int(at.timestamp())),
            "svix-signature": Webhook(self.secret).sign(key, at, raw.decode()),
        }

    async def deliver(
        self, message: EmailMessage, raw: bytes | None = None, headers: dict[str, str] | None = None
    ) -> UUID:
        await DemoEmailAdapter(
            self.service.sessions, self.world.ctx.tenant_id, self.email_id
        ).publish_inbound(self.world.ctx, message)
        if raw is None or headers is None:
            raw, headers = self.notification(message)
        result = await self.service.receive(self.activation_id, raw, headers)
        return result[0].id

    async def work(self, producer: AgentProducer | None = None) -> None:
        runtime = WorkerRuntime(ingress_settings(self.world.url, self.secret), ingress_producer=producer)

        try:
            async with runtime.app.open_async():
                await dispatch_email_ingress(runtime.sessions, runtime.app)
                await runtime.app.run_worker_async(
                    queues=["aco"], concurrency=1, wait=False, listen_notify=False
                )
        finally:
            await runtime.engine.dispose()


@contextmanager
def ingress_world() -> Iterator[IngressWorld]:
    with action_world() as world:
        email = ProviderConnection(
            tenant_id=world.ctx.tenant_id, provider="demo_email", provider_type="email", status="READY"
        )
        storage = ProviderConnection(
            tenant_id=world.ctx.tenant_id, provider="s3", provider_type="storage", status="READY"
        )
        world.store.add_all([email, storage])
        world.store.commit()
        secret = "whsec_" + base64.b64encode(b"synthetic-local-ingress-test-key!!").decode()
        app = cast(FastAPI, world.client.app)
        service = EmailIngressService(app.state.business_sessions, enabled=True, secrets={email.id: secret})
        activation = service.activate(
            world.store,
            world.ctx,
            ActivateIngress(
                email_connection_id=email.id,
                storage_connection_id=storage.id,
                evidence="Explicit isolated synthetic inbound fixture, no live ingress.",
            ),
        )
        world.store.commit()
        yield IngressWorld(world, email.id, storage.id, activation.id, secret, service)


def crash_after_original_identity(url: str, secret: str) -> None:
    """Exit an actual maintained worker after canonical message commit, before private capture."""
    import os

    from aco.features.artifacts.dto import ArtifactRef, ArtifactWrite
    from aco.features.communications.provider_ingress import ProviderIngressRuntime
    from aco.integrations.ports.email_context import EmailIngressContext

    class InterruptedArchive(ArtifactService):
        async def store_inbound_artifact(
            self,
            ctx: EmailIngressContext,
            command: ArtifactWrite,
            *,
            provider_ingress: ProviderIngressRuntime | None = None,
        ) -> ArtifactRef:
            os._exit(87)

    async def run() -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine(url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        artifacts = InterruptedArchive(sessions, StorageRuntime(**storage_configuration()), "local")
        runtime = WorkerRuntime(
            ingress_settings(url, secret), ingress_producer=None, ingress_artifacts=artifacts
        )

        async with runtime.app.open_async():
            await dispatch_email_ingress(runtime.sessions, runtime.app)
            await runtime.app.run_worker_async(queues=["aco"], concurrency=1, wait=False, listen_notify=False)

    asyncio.run(run())


def ingress_settings(url: str, secret: str) -> Settings:
    storage = storage_configuration()
    return Settings(
        database_url=SecretStr(url),
        synthetic_email_ingress_enabled=True,
        synthetic_email_ingress_webhook_secret=SecretStr(secret),
        storage_endpoint=storage["endpoint"],
        storage_bucket=storage["bucket"],
        storage_access_key=storage["access_key"],
        storage_secret_key=storage["secret_key"],
        storage_local_endpoint_ip=storage.get("local_endpoint_ip"),
    )
