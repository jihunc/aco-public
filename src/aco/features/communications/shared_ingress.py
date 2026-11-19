"""Verified account envelope → one current mailbox owner → canonical receipt.

Account event admission and receipt commit atomically. No vendor fields, API
credentials or caller-provided tenant/activation identity select ownership.
"""

from collections.abc import Mapping
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from aco.features.communications.ingress import current_activation, receipt_view
from aco.features.communications.ingress_dto import IngressReceiptView
from aco.features.communications.mailboxes import normalized_mailbox
from aco.features.communications.models import (
    EmailIngressActivation,
    EmailIngressReceipt,
    EmailMailboxClaim,
    EmailWebhookAdmission,
)
from aco.features.identity.models import ProviderConnection
from aco.integrations.ports.email import EmailFailure
from aco.integrations.ports.email_context import EmailIngressContext, EmailWebhookContext
from aco.integrations.ports.email_dto import EmailMessage
from aco.integrations.ports.email_routing import EmailWebhookRoutingPort, VerifiedEmailRoute

if TYPE_CHECKING:
    from aco.features.communications.ingress import EmailIngressService


def target(
    db: Session, service: "EmailIngressService", port: EmailWebhookRoutingPort, route: VerifiedEmailRoute
) -> EmailIngressActivation:
    claims = list(
        db.scalars(
            select(EmailMailboxClaim).where(
                EmailMailboxClaim.address.in_(route.recipients),
                EmailMailboxClaim.released_at.is_(None),
            )
        )
    )
    if {row.address for row in claims} != set(route.recipients):
        raise HTTPException(422, "EMAIL_WEBHOOK_UNKNOWN_RECIPIENT")
    if len({(row.tenant_id, row.email_connection_id, row.activation_id) for row in claims}) != 1:
        raise HTTPException(409, "EMAIL_WEBHOOK_AMBIGUOUS_RECIPIENTS")
    row = current_activation(db, claims[0].activation_id, claims[0].tenant_id)
    runtime = service.provider_ingress
    if runtime is None:
        raise HTTPException(403, "PROVIDER_INGRESS_DISABLED")
    capability = runtime.validate(db, row)
    if capability.evidence_mode == "LIVE":
        from aco.features.communications.email_live import config_for, current_email_live_grant

        assert capability.live_grant_id is not None
        approved = config_for(
            current_email_live_grant(db, capability.live_grant_id, row.tenant_id, row.email_connection_id)
        )
        if route.sender != approved.recipient or route.recipients != (approved.reply_mailbox,):
            raise HTTPException(403, "EMAIL_LIVE_PARTICIPANT_UNAPPROVED")
    connection = db.get(ProviderConnection, row.email_connection_id)
    if (
        connection is None
        or connection.provider != port.provider
        or connection.external_account_id != port.account_id
        or row.secret_sha256 != port.secret_sha256
        or not set(route.recipients).issubset(capability.inbox_addresses)
    ):
        raise HTTPException(403, "EMAIL_WEBHOOK_ACCOUNT_BINDING_MISMATCH")
    return row


async def receive_shared(
    service: "EmailIngressService", endpoint_id: UUID, raw: bytes, headers: Mapping[str, str]
) -> list[IngressReceiptView]:
    runtime = service.provider_ingress
    if runtime is None:
        raise HTTPException(403, "PROVIDER_INGRESS_DISABLED")
    port = service.shared_webhooks.get(endpoint_id)
    if port is None or port.endpoint_id != endpoint_id:
        raise HTTPException(403, "EMAIL_WEBHOOK_ENDPOINT_UNAVAILABLE")
    if len(raw) > 65536:
        raise HTTPException(413, "INGRESS_NOTIFICATION_TOO_LARGE")
    try:
        route = await port.verify_route(raw, headers)
    except EmailFailure:
        raise HTTPException(403, "EMAIL_WEBHOOK_SIGNATURE_OR_PAYLOAD_INVALID") from None
    if route.raw_sha256 != sha256(raw).hexdigest():
        raise HTTPException(403, "EMAIL_WEBHOOK_SIGNATURE_OR_PAYLOAD_INVALID")
    async with service.sessions.begin() as db:
        activation = await db.run_sync(lambda store: target(store, service, port, route))
        ctx = EmailWebhookContext(
            tenant_id=activation.tenant_id,
            actor_id=activation.worker_actor_id,
            correlation_id=uuid4(),
            activation_id=activation.id,
            email_connection_id=activation.email_connection_id,
            raw_sha256=route.raw_sha256,
        )
        adapter = await db.run_sync(lambda store: runtime.port(activation, store))
    # Adapter authorization uses its own transaction. Never hold activation locks across it.
    verified = await adapter.verify_webhook(ctx, raw, headers)
    if len(verified.value) != 1 or (
        verified.value[0].provider_ref.external_id != route.event_id
        or verified.value[0].message_ref.external_id != route.message_id
    ):
        raise HTTPException(403, "EMAIL_WEBHOOK_NOTIFICATION_MISMATCH")
    account_key = port.provider + ":" + port.account_id
    async with service.sessions.begin() as db:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"shared-email:{account_key}:{route.event_id}"},
        )
        activation = await db.run_sync(lambda store: target(store, service, port, route))
        if (activation.id, activation.tenant_id, activation.email_connection_id) != (
            ctx.activation_id,
            ctx.tenant_id,
            ctx.email_connection_id,
        ):
            raise HTTPException(409, "EMAIL_WEBHOOK_OWNER_CHANGED")
        prior = await db.scalar(
            select(EmailWebhookAdmission).where(
                EmailWebhookAdmission.account_id == account_key,
                EmailWebhookAdmission.event_id == route.event_id,
            )
        )
        if prior is not None:
            if (prior.raw_sha256, prior.tenant_id, prior.activation_id, prior.email_connection_id) != (
                route.raw_sha256,
                ctx.tenant_id,
                ctx.activation_id,
                ctx.email_connection_id,
            ):
                raise HTTPException(409, "EMAIL_WEBHOOK_REPLAY_ROUTE_CHANGED")
            receipt = await db.get(EmailIngressReceipt, prior.receipt_id)
            assert receipt is not None
            return [receipt_view(receipt)]
        # Pre-registry receipts lack an immutable account identity. Never move
        # their signed event to a new owner based on today's mailbox routing.
        legacy = list(
            await db.scalars(
                select(EmailIngressReceipt)
                .join(
                    ProviderConnection,
                    ProviderConnection.id == EmailIngressReceipt.email_connection_id,
                )
                .where(
                    ProviderConnection.provider == port.provider,
                    EmailIngressReceipt.event_id == route.event_id,
                )
            )
        )
        if legacy and (
            len(legacy) != 1
            or (
                legacy[0].tenant_id,
                legacy[0].email_connection_id,
                legacy[0].activation_id,
                legacy[0].raw_sha256,
            )
            != (ctx.tenant_id, ctx.email_connection_id, ctx.activation_id, route.raw_sha256)
        ):
            raise HTTPException(409, "EMAIL_WEBHOOK_LEGACY_ROUTE_REVIEW_REQUIRED")
        admitted = await service._admit_verified(db, ctx, raw, headers, verified.value, is_provider=True)
        db.add(
            EmailWebhookAdmission(
                tenant_id=ctx.tenant_id,
                endpoint_id=endpoint_id,
                account_id=account_key,
                event_id=route.event_id,
                recipients=list(route.recipients),
                raw_sha256=route.raw_sha256,
                activation_id=ctx.activation_id,
                email_connection_id=ctx.email_connection_id,
                receipt_id=admitted[0].id,
            )
        )
        await db.flush()
        return admitted


def validate_message_recipients(
    db: Session,
    ctx: EmailIngressContext,
    message: EmailMessage,
    addresses: tuple[str, ...],
) -> None:
    """Admission, normalized provider envelope and original headers cannot disagree."""
    try:
        delivery_to = {normalized_mailbox(item) for item in message.to}
        actual = {normalized_mailbox(item) for item in (*message.to, *message.cc, *message.bcc)}
        allowed = {normalized_mailbox(item) for item in addresses}
        if not delivery_to or not delivery_to.issubset(allowed):
            raise ValueError()
        admission = db.scalar(
            select(EmailWebhookAdmission).where(
                EmailWebhookAdmission.tenant_id == ctx.tenant_id,
                EmailWebhookAdmission.receipt_id == ctx.receipt_id,
            )
        )
        if admission is not None and not delivery_to.issubset(admission.recipients):
            raise ValueError()
        activation = current_activation(db, ctx.activation_id, ctx.tenant_id)
        approved_sender = None
        if (
            activation.provider_binding is not None
            and activation.provider_binding.get("evidence_mode") == "LIVE"
        ):
            from aco.features.communications.email_live import config_for, current_email_live_grant
            from aco.features.communications.provider_ingress import persisted_capability

            cap = persisted_capability(db, activation)
            assert cap.live_grant_id is not None
            approved_sender = config_for(
                current_email_live_grant(db, cap.live_grant_id, ctx.tenant_id, ctx.email_connection_id)
            ).recipient
            if message.sender is None or normalized_mailbox(message.sender) != approved_sender:
                raise ValueError()
        if message.source.content is not None:
            mime = BytesParser(policy=policy.default).parsebytes(message.source.content)
            header_addresses = {
                normalized_mailbox(address)
                for _name, address in getaddresses(
                    [str(value) for key in ("To", "Cc", "Bcc") for value in mime.get_all(key, [])]
                )
            }
            if not header_addresses.issubset(actual):
                raise ValueError()
            if approved_sender is not None:
                original_senders = {
                    normalized_mailbox(address)
                    for _name, address in getaddresses([str(value) for value in mime.get_all("From", [])])
                }
                if original_senders != {approved_sender}:
                    raise ValueError()
            retained_id = mime.get("Message-ID")
            if retained_id and message.message_id and str(retained_id).strip() != message.message_id:
                raise ValueError()
    except (TypeError, ValueError):
        raise HTTPException(403, "INGRESS_FETCHED_ENVELOPE_MISMATCH") from None
