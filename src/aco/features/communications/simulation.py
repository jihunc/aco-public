"""Owner-triggered fixed synthetic source scenarios; no arbitrary mailbox capture or sends."""

from datetime import UTC, timedelta
from email.message import EmailMessage as MimeMessage
from hashlib import sha256
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.core.types import ProviderReference
from aco.features.accounting.models import CustomerContact, InvoiceSnapshot
from aco.features.communications.ingress import EmailIngressService, current_activation
from aco.features.communications.ingress_dto import (
    IngressActivationView,
    IngressConnectionView,
    IngressReceiptView,
    IngressSetupView,
    SimulateIngress,
)
from aco.features.communications.models import EmailIngressActivation
from aco.features.identity.models import ProviderConnection, now
from aco.features.identity.service import IdentityService, Permission
from aco.integrations.demo_email.adapter import DemoEmailAdapter
from aco.integrations.ports.email_dto import EmailMessage, EmailSource


def setup(db: Session, ctx: TenantContext, service: EmailIngressService) -> IngressSetupView:
    if not isinstance(ctx, TenantContext):
        raise HTTPException(403, "HUMAN_REQUIRED")
    tenant = IdentityService(db).authorize(ctx, Permission.SETTINGS)
    enabled = service.enabled and tenant.is_demo
    connections = list(
        db.scalars(
            select(ProviderConnection)
            .where(
                ProviderConnection.tenant_id == tenant.id,
                ProviderConnection.status == "READY",
            )
            .order_by(ProviderConnection.id)
        )
    )
    emails = [
        row
        for row in connections
        if row.provider_type == "email" and row.provider == "demo_email" and service.secret_for(row.id)
    ]
    storage = [row for row in connections if row.provider_type == "storage"]
    activations = []
    if enabled:
        for row in db.scalars(
            select(EmailIngressActivation)
            .where(
                EmailIngressActivation.tenant_id == tenant.id,
                EmailIngressActivation.enabling_member_id == ctx.actor_id,
            )
            .order_by(EmailIngressActivation.created_at.desc())
        ):
            try:
                active = current_activation(db, row.id, tenant.id)
            except HTTPException:
                continue
            secret = service.secret_for(active.email_connection_id)
            if secret and sha256(secret.encode()).hexdigest() == active.secret_sha256:
                activations.append(
                    IngressActivationView.model_validate(active, from_attributes=True).model_copy(
                        update={
                            "synthetic": active.provider_binding is None
                            or active.provider_binding.get("evidence_mode") != "LIVE"
                        }
                    )
                )
    return IngressSetupView(
        enabled=bool(enabled and emails and storage),
        can_prepare=bool(
            enabled
            and service.environment == "local"
            and service.default_secret
            and service.storage_runtime is not None
            and service.storage_runtime.provider == "s3"
        ),
        email_connections=[IngressConnectionView.model_validate(row, from_attributes=True) for row in emails],
        storage_connections=[
            IngressConnectionView.model_validate(row, from_attributes=True) for row in storage
        ],
        activations=activations,
    )


def _scenario_scope(
    db: Session,
    ctx: TenantContext,
    service: EmailIngressService,
    activation_id: UUID,
    command: SimulateIngress,
) -> tuple[UUID, str, list[str], str]:
    if not isinstance(ctx, TenantContext):
        raise HTTPException(403, "HUMAN_REQUIRED")
    IdentityService(db).authorize(ctx, Permission.SETTINGS)
    if not service.enabled:
        raise HTTPException(403, "SYNTHETIC_INGRESS_DISABLED")
    active = current_activation(db, activation_id, ctx.tenant_id)
    if active.enabling_member_id != ctx.actor_id:
        raise HTTPException(403, "INGRESS_ENABLING_OWNER_REQUIRED")
    if active.version != command.expected_activation_version:
        raise HTTPException(409, "INGRESS_ACTIVATION_STALE")
    secret = service.secret_for(active.email_connection_id)
    if not secret or sha256(secret.encode()).hexdigest() != active.secret_sha256:
        raise HTTPException(403, "INGRESS_ACTIVATION_REVOKED")
    rows = list(
        db.scalars(
            select(InvoiceSnapshot)
            .where(
                InvoiceSnapshot.tenant_id == ctx.tenant_id,
                InvoiceSnapshot.id.in_(command.invoice_ids),
            )
            .order_by(InvoiceSnapshot.id)
        )
    )
    if (
        len(set(command.invoice_ids)) != len(command.invoice_ids)
        or len(rows) != len(command.invoice_ids)
        or any(not row.synthetic or not row.verified for row in rows)
        or len({(row.provider_connection_id, row.customer_id) for row in rows}) != 1
    ):
        raise HTTPException(403, "SIMULATION_SEED_SCOPE_REQUIRED")
    if command.scenario == "three_intent_reply" and (
        {row.external_invoice_id for row in rows} != {"INV-101", "INV-107", "INV-113"}
        or {row.number for row in rows} != {"INV-101", "INV-107", "INV-113"}
        or any(row.currency != "AUD" or row.exponent != 2 for row in rows)
    ):
        raise HTTPException(422, "SIMULATION_THREE_INTENT_REQUIRES_CANONICAL_AUD_INVOICES")
    if command.scenario in {"promise_reply", "ambiguous_promise_reply"} and (
        len(rows) != 1 or rows[0].currency != "AUD" or rows[0].exponent != 2
    ):
        raise HTTPException(422, "SIMULATION_PROMISE_REQUIRES_ONE_AUD_INVOICE")
    if command.scenario in {
        "multi_promise_reply",
        "multi_promise_amendment",
        "installment_promise_reply",
    } and (not 2 <= len(rows) <= 3 or any(row.currency != "AUD" or row.exponent != 2 for row in rows)):
        raise HTTPException(422, "SIMULATION_MULTI_PROMISE_REQUIRES_TWO_OR_THREE_AUD_INVOICES")
    accounting = db.get(ProviderConnection, rows[0].provider_connection_id)
    if (
        accounting is None
        or accounting.provider_type != "accounting"
        or accounting.provider != "demo"
        or accounting.status != "READY"
    ):
        raise HTTPException(403, "SIMULATION_SEED_SCOPE_REQUIRED")
    contact = db.scalar(
        select(CustomerContact).where(
            CustomerContact.tenant_id == ctx.tenant_id,
            CustomerContact.provider_connection_id == rows[0].provider_connection_id,
            CustomerContact.customer_id == rows[0].customer_id,
        )
    )
    if contact is None or not contact.email:
        raise HTTPException(403, "SIMULATION_SEED_CONTACT_REQUIRED")
    return active.email_connection_id, secret, [row.number for row in rows], contact.email


def _message(
    ctx: TenantContext,
    connection_id: UUID,
    native_id: str,
    binding: str,
    command: SimulateIngress,
    numbers: list[str],
    contact: str,
) -> EmailMessage:
    received_at = now()
    sender = (
        "unknown@synthetic.invalid"
        if command.scenario in {"unlinked_reply", "unverified_reference_reply"}
        else contact
    )
    body = "SYNTHETIC DEMO SOURCE. This is a fixed application test scenario, not a customer statement. "
    body += (
        "No invoice reference is supplied."
        if command.scenario == "unlinked_reply"
        else "Invoice references: " + ", ".join(numbers) + "."
    )
    if command.scenario == "three_intent_reply":
        due_date = (received_at.astimezone(UTC) + timedelta(days=7)).date().isoformat()
        body += (
            f" For this synthetic example: I commit to pay AUD 100.00 for INV-101 by {due_date} in UTC. "
            "The amount on INV-107 is wrong; please investigate this dispute. "
            "I claim INV-113 was already paid, but provide no payment evidence; verify it against accounting."
        )
    if command.scenario == "ambiguous_promise_reply":
        body += (
            f" For this synthetic example, I commit to pay AUD 40.00 for {numbers[0]} "
            "soon; the date and timezone are not yet agreed."
        )
    if command.scenario == "promise_reply":
        due_date = (received_at.astimezone(UTC) + timedelta(days=7)).date().isoformat()
        body += (
            f" For this synthetic example, I commit to pay AUD 40.00 for {numbers[0]} by {due_date} in UTC."
        )
    if command.scenario in {"multi_promise_reply", "multi_promise_amendment"}:
        amended = command.scenario == "multi_promise_amendment"
        each = 75 if amended else 100
        due_date = (received_at.astimezone(UTC) + timedelta(days=14 if amended else 7)).date().isoformat()
        allocation_text = "; ".join(f"AUD {each}.00 to {number}" for number in numbers)
        body += (
            " For this synthetic example, "
            + ("I revise my earlier commitment and " if amended else "")
            + f"I commit to pay AUD {each * len(numbers)}.00 total by {due_date} in UTC. "
            + f"My explicit invoice allocation is: {allocation_text}."
        )
    if command.scenario == "installment_promise_reply":
        first = (received_at.astimezone(UTC) + timedelta(days=7)).date().isoformat()
        final = (received_at.astimezone(UTC) + timedelta(days=14)).date().isoformat()
        whole = "; ".join(f"AUD 100.00 to {number}" for number in numbers)
        portions = "; ".join(f"AUD 50.00 to {number}" for number in numbers)
        body += (
            f" For this synthetic example, I commit to pay AUD {100 * len(numbers)}.00 total "
            f"by {final} in UTC. My overall explicit invoice allocation is: {whole}. "
            f"Installment 1: AUD {50 * len(numbers)}.00 total by {first} in UTC; "
            f"explicit invoice allocation: {portions}. "
            f"Installment 2: AUD {50 * len(numbers)}.00 total by {final} in UTC; "
            f"explicit invoice allocation: {portions}."
        )
    if command.scenario == "contact_stop_reply":
        body += (
            " For this synthetic example: Please stop all contact for this customer "
            "until a human reviews this request."
        )
    if command.scenario == "case_reply":
        body += (
            " For this synthetic example: The purchase order reference is wrong for these invoices. "
            "Please investigate the PO mismatch."
        )
    if command.scenario == "case_resolution":
        body += (
            " For this synthetic example: The purchase order reference is now confirmed correct "
            "for these invoices. The documentary PO issue is resolved; this is not a payment."
        )
    mime = MimeMessage()
    mime["From"], mime["To"], mime["Subject"] = (
        sender,
        "collections@synthetic.invalid",
        "SYNTHETIC inbound demo",
    )
    mime["Message-ID"] = f"<{command.request_id}@synthetic.invalid>"
    mime["X-ACO-Simulation-Command"] = binding
    mime.set_content(body)
    raw = mime.as_bytes()
    return EmailMessage.model_validate(
        dict(
            tenant_id=ctx.tenant_id,
            provider_connection_id=connection_id,
            provider_ref=ProviderReference(
                provider="demo_email", connection_id=connection_id, external_id=native_id
            ),
            provider_thread_ref=ProviderReference(
                provider="demo_email", connection_id=connection_id, external_id=f"thread:{native_id}"
            ),
            native_identity="SIMULATED",
            synthetic=True,
            authenticity="SIMULATED",
            direction="incoming",
            kind="OUTBOUND_ECHO" if command.scenario == "outbound_echo" else "CUSTOMER_REPLY",
            received_at=received_at,
            sender=sender,
            to=("collections@synthetic.invalid",),
            subject=str(mime["Subject"]),
            text_body=body,
            message_id=str(mime["Message-ID"]),
            headers=tuple(mime.items()),
            source=EmailSource(
                kind="PROVIDER_ORIGINAL", content=raw, integrity_sha256=sha256(raw).hexdigest()
            ),
        )
    )


async def simulate(
    service: EmailIngressService, ctx: TenantContext, activation_id: UUID, command: SimulateIngress
) -> IngressReceiptView:
    async with service.sessions.begin() as db:
        connection_id, secret, numbers, contact = await db.run_sync(
            lambda store: _scenario_scope(store, ctx, service, activation_id, command)
        )
    native_id = f"simulation:{activation_id}:{command.request_id}"
    binding = command.model_dump_json()
    adapter = DemoEmailAdapter(service.sessions, ctx.tenant_id, connection_id)
    message = await adapter.publish_fixture(
        ctx,
        native_id,
        binding,
        lambda: _message(ctx, connection_id, native_id, binding, command, numbers, contact),
    )
    assert message.received_at is not None
    raw, headers = adapter.sign_fixture_notification(message, f"notification:{native_id}", secret)
    return (await service.receive(activation_id, raw, headers))[0]
