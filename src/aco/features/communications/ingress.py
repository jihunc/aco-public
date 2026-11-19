"""Persisted synthetic callback authority and maintained-job receipt outbox."""

import base64
from collections.abc import Mapping
from datetime import timedelta
from functools import partial
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from fastapi import HTTPException
from procrastinate import App
from procrastinate.jobs import Job
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.features.communications.ingress_dto import ActivateIngress, IngressActivationView, IngressReceiptView
from aco.features.communications.models import EmailIngressActivation, EmailIngressReceipt
from aco.features.identity.models import Actor, Membership, ProviderConnection, Tenant, now
from aco.features.identity.service import IdentityService, Permission
from aco.integrations.ports.email_context import EmailIngressContext, EmailWebhookContext
from aco.integrations.ports.email_dto import EmailNotification
from aco.integrations.ports.email_routing import EmailWebhookRoutingPort
from aco.integrations.ports.storage import StorageRuntime

if TYPE_CHECKING:
    from aco.features.communications.provider_ingress import ProviderIngressRuntime


def receipt_view(row: EmailIngressReceipt) -> IngressReceiptView:
    return IngressReceiptView.model_validate(row, from_attributes=True).model_copy(
        update={"synthetic": row.notification.get("synthetic") is True}
    )


def audit_notification(db: Session, ctx: EmailWebhookContext, key: UUID) -> None:
    row = db.get(EmailIngressReceipt, key)
    assert row is not None and row.tenant_id == ctx.tenant_id
    IdentityService(db).audit(
        ctx, "email.notification_verified", key, {"synthetic": row.notification.get("synthetic") is True}
    )


def current_activation(db: Session, key: UUID, tenant_id: UUID | None = None) -> EmailIngressActivation:
    row = db.get(EmailIngressActivation, key, populate_existing=True)
    if row is None or (tenant_id is not None and row.tenant_id != tenant_id):
        raise HTTPException(403, "INGRESS_ACTIVATION_INVALID")
    tenant = db.get(Tenant, row.tenant_id, populate_existing=True, with_for_update={"key_share": True})
    row = db.get(EmailIngressActivation, key, populate_existing=True, with_for_update=True)
    assert row is not None
    member = db.scalar(
        select(Membership)
        .where(Membership.tenant_id == row.tenant_id, Membership.user_id == row.enabling_member_id)
        .execution_options(populate_existing=True)
    )
    actor = db.get(Actor, row.worker_actor_id, populate_existing=True)
    email = db.get(ProviderConnection, row.email_connection_id, populate_existing=True)
    storage = db.get(ProviderConnection, row.storage_connection_id, populate_existing=True)
    if (
        tenant is None
        or not tenant.is_demo
        or tenant.version != row.tenant_generation
        or row.revoked_at is not None
        or row.expires_at <= now()
        or member is None
        or not member.active
        or member.role != "owner"
        or member.version != row.membership_version
        or actor is None
        or not actor.active
        or actor.kind != "service"
        or actor.purpose != "durable_worker"
        or actor.tenant_id != row.tenant_id
        or actor.version != row.actor_version
        or email is None
        or email.tenant_id != row.tenant_id
        or email.provider != ("demo_email" if row.provider_binding is None else "resend")
        or email.provider_type != "email"
        or email.status != "READY"
        or email.version != row.email_connection_version
        or storage is None
        or storage.tenant_id != row.tenant_id
        or storage.provider_type != "storage"
        or storage.status != "READY"
        or storage.version != row.storage_connection_version
    ):
        raise HTTPException(403, "INGRESS_ACTIVATION_REVOKED")
    if row.provider_binding is not None:
        from aco.features.communications.provider_ingress import persisted_capability

        persisted_capability(db, row)
    return row


def validate_webhook(
    db: Session, ctx: EmailWebhookContext, raw: bytes, secret: str
) -> EmailIngressActivation:
    row = current_activation(db, ctx.activation_id, ctx.tenant_id)
    if (
        ctx.email_connection_id != row.email_connection_id
        or ctx.actor_id != row.worker_actor_id
        or ctx.raw_sha256 != sha256(raw).hexdigest()
        or row.secret_sha256 != sha256(secret.encode()).hexdigest()
    ):
        raise HTTPException(403, "INGRESS_WEBHOOK_SCOPE")
    return row


def current_receipt(db: Session, ctx: EmailIngressContext) -> EmailIngressReceipt:
    activation = current_activation(db, ctx.activation_id, ctx.tenant_id)
    row = db.get(EmailIngressReceipt, ctx.receipt_id, populate_existing=True, with_for_update=True)
    job = db.execute(
        text("SELECT task_name,args,status::text,worker_id FROM procrastinate_jobs WHERE id=:id"),
        {"id": ctx.job_id},
    ).first()
    worker = (
        None
        if job is None or job.worker_id is None
        else db.execute(
            text("SELECT last_heartbeat FROM procrastinate_workers WHERE id=:id"), {"id": job.worker_id}
        ).first()
    )
    if (
        row is None
        or row.tenant_id != ctx.tenant_id
        or row.activation_id != activation.id
        or row.email_connection_id != ctx.email_connection_id
        or row.message_id != ctx.message_id
        or row.state != "fetching"
        or row.job_version != ctx.receipt_version
        or row.job_id != ctx.job_id
        or row.lease_token != ctx.lease_token
        or row.lease_until is None
        or row.lease_until <= now()
        or ctx.actor_id != activation.worker_actor_id
        or ctx.storage_connection_id != activation.storage_connection_id
        or row.correlation_id != ctx.correlation_id
        or job is None
        or job.task_name != "aco.ingest_email"
        or job.status != "doing"
        or job.args != {"receipt_id": str(row.id), "expected_version": row.job_version}
        or worker is None
        or worker.last_heartbeat < now() - timedelta(seconds=90)
    ):
        raise HTTPException(403, "INGRESS_JOB_AUTHORITY_INVALID")
    return row


class EmailIngressService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        enabled: bool = False,
        secrets: Mapping[UUID, str] | None = None,
        default_secret: str | None = None,
        storage_runtime: StorageRuntime | None = None,
        environment: str = "local",
        provider_ingress: "ProviderIngressRuntime | None" = None,
        shared_webhooks: Mapping[UUID, EmailWebhookRoutingPort] | None = None,
    ):
        self.sessions, self.enabled, self.secrets = sessions, enabled, secrets or {}
        self.default_secret = default_secret
        self.storage_runtime, self.environment = storage_runtime, environment
        self.provider_ingress = provider_ingress
        self.shared_webhooks = dict(shared_webhooks or {})

    def secret_for(self, connection_id: UUID) -> str | None:
        # Only callers that freshly validate a synthetic DemoEmail connection may use this.
        return self.secrets.get(connection_id) or self.default_secret

    def activate(self, db: Session, ctx: TenantContext, command: ActivateIngress) -> IngressActivationView:
        if not isinstance(ctx, TenantContext):
            raise HTTPException(403, "HUMAN_REQUIRED")
        db.get(Tenant, ctx.tenant_id, with_for_update={"key_share": True}, populate_existing=True)
        tenant = IdentityService(db).authorize(ctx, Permission.SETTINGS)
        if not self.enabled or not tenant.is_demo:
            raise HTTPException(403, "SYNTHETIC_INGRESS_DISABLED")
        email = db.get(ProviderConnection, command.email_connection_id)
        storage = db.get(ProviderConnection, command.storage_connection_id)
        secret = self.secret_for(command.email_connection_id)
        if (
            email is None
            or email.tenant_id != tenant.id
            or email.provider != "demo_email"
            or email.provider_type != "email"
            or email.status != "READY"
            or not secret
            or storage is None
            or storage.tenant_id != tenant.id
            or storage.provider_type != "storage"
            or storage.status != "READY"
        ):
            raise HTTPException(403, "INGRESS_CONNECTION_INVALID")
        member = db.scalar(
            select(Membership).where(Membership.tenant_id == tenant.id, Membership.user_id == ctx.actor_id)
        )
        assert member is not None
        actor = db.scalar(
            select(Actor).where(Actor.tenant_id == tenant.id, Actor.purpose == "durable_worker")
        )
        if actor is None:
            actor = Actor(kind="service", tenant_id=tenant.id, purpose="durable_worker", active=True)
            db.add(actor)
            db.flush()
        if not actor.active:
            raise HTTPException(403, "INGRESS_WORKER_REVOKED")
        row = EmailIngressActivation(
            tenant_id=tenant.id,
            email_connection_id=email.id,
            email_connection_version=email.version,
            storage_connection_id=storage.id,
            storage_connection_version=storage.version,
            enabling_member_id=ctx.actor_id,
            membership_version=member.version,
            worker_actor_id=actor.id,
            actor_version=actor.version,
            tenant_generation=tenant.version,
            secret_sha256=sha256(secret.encode()).hexdigest(),
            evidence=command.evidence,
            expires_at=now() + timedelta(seconds=command.expires_in_seconds),
        )
        db.add(row)
        db.flush()
        IdentityService(db).audit(ctx, "email.ingress_activated", row.id, {"synthetic": True})
        return IngressActivationView.model_validate(row, from_attributes=True).model_copy(
            update={
                "synthetic": row.provider_binding is None
                or row.provider_binding.get("evidence_mode") != "LIVE"
            }
        )

    def revoke(
        self, db: Session, ctx: TenantContext, key: UUID, expected_version: int
    ) -> IngressActivationView:
        if not isinstance(ctx, TenantContext):
            raise HTTPException(403, "HUMAN_REQUIRED")
        db.get(Tenant, ctx.tenant_id, with_for_update={"key_share": True}, populate_existing=True)
        IdentityService(db).authorize(ctx, Permission.SETTINGS)
        row = db.get(EmailIngressActivation, key, with_for_update=True, populate_existing=True)
        if row is None or row.tenant_id != ctx.tenant_id:
            raise HTTPException(404, "INGRESS_ACTIVATION_NOT_FOUND")
        if row.version != expected_version:
            raise HTTPException(409, "INGRESS_ACTIVATION_CONFLICT")
        if row.revoked_at is None:
            row.revoked_at = now()
            row.version += 1
            from aco.features.communications.mailboxes import release_mailboxes

            release_mailboxes(db, row.id)
            IdentityService(db).audit(ctx, "email.ingress_revoked", row.id, {"synthetic": True})
        return IngressActivationView.model_validate(row, from_attributes=True).model_copy(
            update={
                "synthetic": row.provider_binding is None
                or row.provider_binding.get("evidence_mode") != "LIVE"
            }
        )

    def retry(self, db: Session, ctx: TenantContext, key: UUID, expected_version: int) -> IngressReceiptView:
        if not isinstance(ctx, TenantContext):
            raise HTTPException(403, "HUMAN_REQUIRED")
        db.get(Tenant, ctx.tenant_id, with_for_update={"key_share": True}, populate_existing=True)
        IdentityService(db).authorize(ctx, Permission.SETTINGS)
        row = db.get(EmailIngressReceipt, key, with_for_update=True, populate_existing=True)
        if row is None or row.tenant_id != ctx.tenant_id:
            raise HTTPException(404, "INGRESS_RECEIPT_NOT_FOUND")
        if row.state != "failed" or row.version != expected_version:
            raise HTTPException(409, "INGRESS_RETRY_CONFLICT")
        activation = current_activation(db, row.activation_id, ctx.tenant_id)
        if activation.provider_binding is not None:
            if self.provider_ingress is None:
                raise HTTPException(403, "PROVIDER_INGRESS_DISABLED")
            self.provider_ingress.validate(db, activation)
        elif not self.enabled:
            raise HTTPException(403, "SYNTHETIC_INGRESS_DISABLED")
        row.state, row.error_code, row.job_id = "queued", None, None
        row.lease_token, row.lease_until = None, None
        row.job_version += 1
        row.version += 1
        IdentityService(db).audit(ctx, "email.ingress_retry", row.id, {"synthetic": True})
        return receipt_view(row)

    async def receive(
        self, activation_id: UUID, raw: bytes, headers: Mapping[str, str]
    ) -> list[IngressReceiptView]:
        from aco.integrations.demo_email.adapter import DemoEmailAdapter

        if not self.enabled and self.provider_ingress is None:
            raise HTTPException(403, "SYNTHETIC_INGRESS_DISABLED")
        if len(raw) > 65536:
            raise HTTPException(413, "INGRESS_NOTIFICATION_TOO_LARGE")
        async with self.sessions.begin() as db:
            activation = await db.run_sync(lambda store: current_activation(store, activation_id))
            is_provider = activation.provider_binding is not None
            if is_provider:
                connection = await db.get(ProviderConnection, activation.email_connection_id)
                if connection is not None and connection.external_account_id is not None:
                    raise HTTPException(403, "EMAIL_SHARED_WEBHOOK_REQUIRED")
            secret = None
            if not is_provider:
                if not self.enabled:
                    raise HTTPException(403, "SYNTHETIC_INGRESS_DISABLED")
                secret = self.secret_for(activation.email_connection_id)
                if not secret:
                    raise HTTPException(403, "INGRESS_SECRET_UNAVAILABLE")
            elif self.provider_ingress is None:
                raise HTTPException(403, "PROVIDER_INGRESS_DISABLED")
            ctx = EmailWebhookContext(
                tenant_id=activation.tenant_id,
                actor_id=activation.worker_actor_id,
                correlation_id=uuid4(),
                activation_id=activation.id,
                email_connection_id=activation.email_connection_id,
                raw_sha256=sha256(raw).hexdigest(),
            )
            if is_provider:
                provider_port_runtime = self.provider_ingress
                assert provider_port_runtime is not None
                adapter = await db.run_sync(lambda store: provider_port_runtime.port(activation, store))
            else:
                adapter = DemoEmailAdapter(
                    self.sessions, ctx.tenant_id, ctx.email_connection_id, webhook_secret=secret
                )
        verified = await adapter.verify_webhook(ctx, raw, headers)
        async with self.sessions.begin() as db:
            return await self._admit_verified(db, ctx, raw, headers, verified.value, is_provider=is_provider)

    async def _admit_verified(
        self,
        db: AsyncSession,
        ctx: EmailWebhookContext,
        raw: bytes,
        headers: Mapping[str, str],
        notifications: tuple[EmailNotification, ...],
        *,
        is_provider: bool,
    ) -> list[IngressReceiptView]:
        provider_capability = None
        if is_provider:
            provider_ingress = self.provider_ingress
            assert provider_ingress is not None
            provider_capability = await db.run_sync(
                lambda store: provider_ingress.validate(
                    store, current_activation(store, ctx.activation_id, ctx.tenant_id)
                )
            )
        else:
            secret = self.secret_for(ctx.email_connection_id)
            if not secret:
                raise HTTPException(403, "INGRESS_SECRET_UNAVAILABLE")
            await db.run_sync(lambda store: validate_webhook(store, ctx, raw, secret))
        # Serialize exact connection event admission, independent of invoice scope.
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"email-ingress:{ctx.email_connection_id}"},
        )
        results = []
        for event in notifications:
            if (
                event.tenant_id != ctx.tenant_id
                or event.provider_connection_id != ctx.email_connection_id
                or event.provider_ref.provider != ("resend" if is_provider else "demo_email")
                or event.synthetic
                != (provider_capability is None or provider_capability.evidence_mode == "MOCKED_HTTP")
                or event.native_identity
                != (
                    "PROVIDER"
                    if provider_capability is not None and provider_capability.evidence_mode == "LIVE"
                    else "SIMULATED"
                )
            ):
                raise HTTPException(403, "INGRESS_NOTIFICATION_PROVENANCE")
            row = await db.scalar(
                select(EmailIngressReceipt).where(
                    EmailIngressReceipt.tenant_id == ctx.tenant_id,
                    EmailIngressReceipt.email_connection_id == ctx.email_connection_id,
                    EmailIngressReceipt.event_id == event.provider_ref.external_id,
                )
            )
            if row is not None:
                if row.raw_sha256 != ctx.raw_sha256 or row.notification != event.model_dump(mode="json"):
                    raise HTTPException(409, "INGRESS_EVENT_IDENTITY_CONFLICT")
            else:
                if provider_capability is not None and provider_capability.evidence_mode == "LIVE":
                    from aco.features.communications.email_live import config_for, current_email_live_grant

                    live_grant_id = provider_capability.live_grant_id
                    assert live_grant_id is not None
                    grant = await db.run_sync(
                        partial(
                            current_email_live_grant,
                            grant_id=live_grant_id,
                            tenant_id=ctx.tenant_id,
                            connection_id=ctx.email_connection_id,
                        )
                    )
                    admitted = set(
                        await db.scalars(
                            select(EmailIngressReceipt.message_id)
                            .join(
                                EmailIngressActivation,
                                EmailIngressActivation.id == EmailIngressReceipt.activation_id,
                            )
                            .where(
                                EmailIngressReceipt.tenant_id == ctx.tenant_id,
                                EmailIngressReceipt.email_connection_id == ctx.email_connection_id,
                                EmailIngressActivation.provider_binding["live_grant_id"].as_string()
                                == str(grant.id),
                            )
                        )
                    )
                    if (
                        event.message_ref.external_id not in admitted
                        and len(admitted) >= config_for(grant).max_received_messages
                    ):
                        raise HTTPException(403, "EMAIL_LIVE_RECEIVED_MESSAGE_CAP")
                row = EmailIngressReceipt(
                    tenant_id=ctx.tenant_id,
                    activation_id=ctx.activation_id,
                    email_connection_id=ctx.email_connection_id,
                    event_id=event.provider_ref.external_id,
                    message_id=event.message_ref.external_id,
                    raw_sha256=ctx.raw_sha256,
                    notification=event.model_dump(mode="json"),
                    signed_raw_body=base64.b64encode(raw).decode(),
                    signature_headers={
                        key: headers[key] for key in ("svix-id", "svix-timestamp", "svix-signature")
                    },
                    correlation_id=ctx.correlation_id,
                )
                db.add(row)
                await db.flush()
                await db.run_sync(audit_notification, ctx, row.id)
            results.append(receipt_view(row))
        return results

    async def receive_shared(
        self,
        endpoint_id: UUID,
        raw: bytes,
        headers: Mapping[str, str],
    ) -> list[IngressReceiptView]:
        from aco.features.communications.shared_ingress import receive_shared

        return await receive_shared(self, endpoint_id, raw, headers)


async def dispatch_email_ingress(sessions: async_sessionmaker[AsyncSession], app: App) -> int:
    async with sessions.begin() as db:
        rows = list(
            await db.scalars(
                select(EmailIngressReceipt)
                .where(EmailIngressReceipt.state == "queued", EmailIngressReceipt.job_id.is_(None))
                .order_by(EmailIngressReceipt.created_at)
                .limit(20)
                .with_for_update(skip_locked=True)
            )
        )
        raw = await (await db.connection()).get_raw_connection()
        for row in rows:
            job = await app.job_manager.defer_job_async(
                Job(
                    task_name="aco.ingest_email",
                    queue="aco",
                    lock=None,
                    queueing_lock=None,
                    task_kwargs={"receipt_id": str(row.id), "expected_version": row.job_version},
                ),
                connection=raw.driver_connection,
            )
            row.job_id = job.id
        return len(rows)


async def recover_email_ingress(sessions: async_sessionmaker[AsyncSession]) -> int:
    """Recover stale library jobs through the same receipt outbox, retaining original identity."""
    count = 0
    async with sessions.begin() as db:
        rows = list(
            await db.scalars(
                select(EmailIngressReceipt)
                .where(
                    EmailIngressReceipt.state.in_(["queued", "fetching"]),
                    EmailIngressReceipt.job_id.is_not(None),
                )
                .with_for_update(skip_locked=True)
                .limit(100)
            )
        )
        for row in rows:
            job = (
                await db.execute(
                    text("SELECT status::text, worker_id FROM procrastinate_jobs WHERE id=:id"),
                    {"id": row.job_id},
                )
            ).first()
            heartbeat = (
                None
                if job is None or job.worker_id is None
                else (
                    await db.execute(
                        text("SELECT last_heartbeat FROM procrastinate_workers WHERE id=:id"),
                        {"id": job.worker_id},
                    )
                ).scalar_one_or_none()
            )
            stale = (
                row.state == "fetching"
                and row.lease_until is not None
                and row.lease_until <= now()
                and (heartbeat is None or heartbeat < now() - timedelta(seconds=90))
            )
            terminal = job is None or job.status in {"failed", "succeeded", "aborted"}
            if not stale and not terminal:
                continue
            if row.job_version >= 3:
                row.state, row.error_code = "failed", "INGRESS_RETRY_LIMIT"
            else:
                row.state, row.job_id, row.error_code = "queued", None, None
                row.job_version += 1
            row.lease_token, row.lease_until = None, None
            row.version += 1
            count += 1
    return count
