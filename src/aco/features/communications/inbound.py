"""Exact maintained-job fetch, private original capture and conservative source linkage."""

from collections.abc import Callable
from datetime import timedelta
from email import policy
from email.parser import BytesParser
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from aco.core.types import ProviderReference
from aco.features.artifacts.dto import ArtifactWrite
from aco.features.artifacts.service import ArtifactService
from aco.features.communications.ingress import current_activation, current_receipt
from aco.features.communications.ingress_dto import InboundAgentSource
from aco.features.communications.linking import invoice_evidence, resolve_thread, sanitized_html
from aco.features.communications.models import (
    Communication,
    CommunicationInvoiceLink,
    CommunicationSource,
    CommunicationThread,
    EmailIngressReceipt,
)
from aco.features.identity.models import Tenant, now
from aco.integrations.demo_email.adapter import DemoEmailAdapter
from aco.integrations.ports.email_context import EmailIngressContext
from aco.integrations.ports.email_dto import EmailAttachment, EmailContent, EmailMessage, EmailObjectReference

if TYPE_CHECKING:
    from aco.features.communications.provider_ingress import ProviderIngressRuntime


AgentProducer = Callable[[Session, EmailIngressContext, InboundAgentSource], UUID | None]


def capture_commands(message: EmailMessage, communication_id: UUID) -> list[ArtifactWrite]:
    source = message.source
    if source.kind != "PROVIDER_ORIGINAL" or source.content is None:
        return []
    body = source.content
    commands = [
        ArtifactWrite(
            artifact_id=uuid5(communication_id, "original"),
            source_kind="provider_message",
            source_reference=f"inbound:{communication_id}:original",
            content_type=source.content_type,
            body=body,
            capture_class="synthetic_email" if message.synthetic else "real",
            captured_at=message.received_at or message.sent_at,
        )
    ]
    # Parse with Python's maintained MIME parser; no remote resources or attachment references are fetched.
    mime = BytesParser(policy=policy.default).parsebytes(body)
    for index, part in enumerate(mime.iter_attachments()):
        attachment = part.get_payload(decode=True)
        if not isinstance(attachment, bytes):
            continue
        commands.append(
            ArtifactWrite(
                artifact_id=uuid5(communication_id, f"attachment:{index}"),
                source_kind="provider_message",
                source_reference=f"inbound:{communication_id}:attachment:{index}",
                content_type=part.get_content_type(),
                body=attachment,
                capture_class="synthetic_email" if message.synthetic else "real",
                captured_at=message.received_at or message.sent_at,
            )
        )
    return commands


def capture_manifest(command: ArtifactWrite) -> dict[str, object]:
    return {
        "artifact_id": str(command.artifact_id),
        "source_reference": command.source_reference,
        "sha256": sha256(command.body).hexdigest(),
        "content_type": command.content_type,
        "size_bytes": len(command.body),
    }


def validate_capture(
    db: Session,
    ctx: EmailIngressContext,
    command: ArtifactWrite,
    *,
    provider_ingress: "ProviderIngressRuntime | None" = None,
) -> Tenant:
    receipt = current_receipt(db, ctx)
    activation = current_activation(db, ctx.activation_id, ctx.tenant_id)
    expected_class = "synthetic_email"
    if activation.provider_binding is not None:
        if provider_ingress is None:
            raise HTTPException(403, "PROVIDER_INGRESS_DISABLED")
        capability = provider_ingress.validate(db, activation)
        if capability.evidence_mode == "LIVE":
            expected_class = "real"
    if (
        command.capture_class != expected_class
        or command.source_kind != "provider_message"
        or command.artifact_id not in ctx.artifact_ids
        or capture_manifest(command) not in receipt.capture_objects
    ):
        raise HTTPException(403, "INGRESS_CAPTURE_SCOPE")
    tenant = db.get(Tenant, receipt.tenant_id)
    assert tenant is not None
    return tenant


def persist_message(
    db: Session, ctx: EmailIngressContext, message: EmailMessage
) -> tuple[Communication, list[ArtifactWrite]]:
    receipt = current_receipt(db, ctx)
    activation = current_activation(db, ctx.activation_id, ctx.tenant_id)
    provider = "resend" if activation.provider_binding is not None else "demo_email"
    synthetic = True
    if activation.provider_binding is not None:
        from aco.features.communications.provider_ingress import persisted_capability
        from aco.features.communications.shared_ingress import validate_message_recipients

        capability = persisted_capability(db, activation)
        synthetic = capability.evidence_mode == "MOCKED_HTTP"
        validate_message_recipients(db, ctx, message, capability.inbox_addresses)
    if (
        message.tenant_id != ctx.tenant_id
        or message.provider_connection_id != ctx.email_connection_id
        or message.provider_ref
        != ProviderReference(
            provider=provider, connection_id=ctx.email_connection_id, external_id=ctx.message_id
        )
        or message.synthetic != synthetic
        or message.native_identity != ("SIMULATED" if synthetic else "PROVIDER")
    ):
        raise HTTPException(403, "INGRESS_MESSAGE_PROVENANCE")
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
        {"key": f"inbound-message:{ctx.tenant_id}:{ctx.email_connection_id}:{ctx.message_id}"},
    )
    row = db.scalar(
        select(Communication).where(
            Communication.tenant_id == ctx.tenant_id,
            Communication.provider_connection_id == ctx.email_connection_id,
            Communication.native_message_id == ctx.message_id,
        )
    )
    digest = sha256(message.model_dump_json().encode()).hexdigest()
    if row is None:
        links = invoice_evidence(db, message)
        resolution = resolve_thread(db, message)
        thread = resolution.thread
        native_thread = (
            message.provider_thread_ref.external_id
            if message.provider_thread_ref and not resolution.ambiguous
            else None
        )
        if (
            thread is not None
            and thread.customer_id is not None
            and any(e.customer_id != thread.customer_id for e in links)
        ):
            links = []
        if thread is None:
            thread = CommunicationThread(
                tenant_id=ctx.tenant_id,
                provider_connection_id=ctx.email_connection_id,
                origin_invoice_id=links[0].invoice_id if links else None,
                customer_id=links[0].customer_id if links else None,
                purpose="provider_inbound",
                native_thread_id=native_thread,
            )
            db.add(thread)
            db.flush()
        row = Communication(
            tenant_id=ctx.tenant_id,
            provider_connection_id=ctx.email_connection_id,
            thread_id=thread.id,
            origin_invoice_id=thread.origin_invoice_id,
            operation_id=uuid5(
                NAMESPACE_URL, f"aco:inbound:{ctx.tenant_id}:{ctx.email_connection_id}:{ctx.message_id}"
            ),
            native_message_id=ctx.message_id,
            direction=message.direction,
            kind=message.kind,
            synthetic=synthetic,
            content={},
            event_at=message.received_at or message.sent_at,
            delivery_state="received",
            source_state="pending",
            message_sha256=digest,
            headers=[list(header) for header in message.headers],
        )
        db.add(row)
        db.flush()
        content = {key: getattr(message, key) for key in EmailContent.model_fields}
        content["html_body"] = sanitized_html(message.html_body)
        commands = capture_commands(message, row.id)
        mime = BytesParser(policy=policy.default).parsebytes(message.source.content or b"")
        attachments = list(mime.iter_attachments())
        content["attachments"] = (
            tuple(
                EmailAttachment(
                    filename=attachments[index].get_filename() or "attachment",
                    content_type=cmd.content_type,
                    reference=EmailObjectReference(artifact_id=cmd.artifact_id),
                    integrity_sha256=sha256(cmd.body).hexdigest(),
                    size_bytes=len(cmd.body),
                    disposition="attachment",
                )
                for index, cmd in enumerate(commands[1:])
            )
            if commands
            else message.attachments
        )
        row.content = EmailContent.model_validate(content).model_dump(mode="json")
        for evidence in links:
            db.add(
                CommunicationInvoiceLink(
                    tenant_id=ctx.tenant_id,
                    communication_id=row.id,
                    invoice_id=evidence.invoice_id,
                    claim_or_context_key=f"ingress:{evidence.provenance}:{evidence.offset}",
                    provenance=evidence.provenance,
                    confidence="contextual_unverified_sender",
                    evidence_excerpt=evidence.excerpt,
                    evidence_offset=evidence.offset,
                    classification="review_required",
                )
            )
    elif row.message_sha256 is not None and row.message_sha256 != digest:
        raise HTTPException(409, "INGRESS_MESSAGE_IDENTITY_CONFLICT")
    # Legacy outgoing echo belongs to its original outbound record, never creates a reply/run.
    commands = capture_commands(message, row.id) if row.direction == "incoming" else []
    receipt.communication_id = row.id
    receipt.capture_objects = [capture_manifest(command) for command in commands]
    db.flush()
    return row, commands


async def execute_email_ingress(
    sessions: async_sessionmaker[AsyncSession],
    receipt_id: UUID,
    expected_version: int,
    job_id: int,
    artifacts: ArtifactService,
    producer: AgentProducer | None = None,
    provider_ingress: "ProviderIngressRuntime | None" = None,
) -> None:
    async with sessions.begin() as db:
        row = await db.get(EmailIngressReceipt, receipt_id)
        if (
            row is None
            or row.state != "queued"
            or row.job_id != job_id
            or row.job_version != expected_version
        ):
            return
        activation_id, tenant_id = row.activation_id, row.tenant_id
        try:
            activation = await db.run_sync(lambda store: current_activation(store, activation_id, tenant_id))
        except HTTPException as error:
            row.state, row.error_code = "failed", str(error.detail)
            row.version += 1
            return
        claimed = await db.get(EmailIngressReceipt, receipt_id, with_for_update=True, populate_existing=True)
        assert claimed is not None
        row = claimed
        if row.state != "queued" or row.job_id != job_id or row.job_version != expected_version:
            return
        row.state, row.lease_token, row.lease_until = "fetching", uuid4(), now() + timedelta(seconds=90)
        row.version += 1
        ctx = EmailIngressContext(
            tenant_id=row.tenant_id,
            actor_id=activation.worker_actor_id,
            correlation_id=row.correlation_id,
            activation_id=activation.id,
            receipt_id=row.id,
            receipt_version=row.job_version,
            email_connection_id=row.email_connection_id,
            message_id=row.message_id,
            job_id=job_id,
            lease_token=row.lease_token,
            storage_connection_id=activation.storage_connection_id,
        )
        await db.flush()
        await db.run_sync(lambda store: current_receipt(store, ctx))
        provider = "demo_email"
        if activation.provider_binding is not None:
            if provider_ingress is None:
                raise HTTPException(403, "PROVIDER_INGRESS_DISABLED")
            adapter = await db.run_sync(lambda store: provider_ingress.port(activation, store))
            provider = "resend"
        else:
            adapter = DemoEmailAdapter(sessions, ctx.tenant_id, ctx.email_connection_id)
    try:
        result = await adapter.get_message(
            ctx,
            ProviderReference(
                provider=provider, connection_id=ctx.email_connection_id, external_id=ctx.message_id
            ),
        )
        async with sessions.begin() as db:
            if provider == "resend":
                assert provider_ingress is not None
                await db.run_sync(
                    lambda store: provider_ingress.validate(
                        store, current_activation(store, ctx.activation_id, ctx.tenant_id)
                    )
                )
            communication, commands = await db.run_sync(
                lambda store: persist_message(store, ctx, result.value)
            )
            communication_id = communication.id
        ctx = ctx.model_copy(update={"artifact_ids": tuple(command.artifact_id for command in commands)})
        for index, command in enumerate(commands):
            if provider == "resend":
                assert provider_ingress is not None
                await provider_ingress.archive_authority(ctx)
                artifact = await artifacts.store_inbound_artifact(
                    ctx, command, provider_ingress=provider_ingress
                )
            else:
                artifact = await artifacts.store_inbound_artifact(ctx, command)
            async with sessions.begin() as db:
                await db.run_sync(lambda store: current_receipt(store, ctx))
                prior = await db.scalar(
                    select(CommunicationSource).where(
                        CommunicationSource.tenant_id == ctx.tenant_id,
                        CommunicationSource.communication_id == communication_id,
                        CommunicationSource.artifact_id == artifact.id,
                    )
                )
                if prior is None:
                    db.add(
                        CommunicationSource(
                            tenant_id=ctx.tenant_id,
                            communication_id=communication_id,
                            artifact_id=artifact.id,
                            representation="provider_original" if index == 0 else "attachment",
                            sha256=artifact.sha256,
                            evidence={
                                "receipt_id": str(receipt_id),
                                "synthetic": result.value.synthetic,
                                "provider_evidence_mode": (
                                    "MOCKED_HTTP" if result.value.synthetic else "LIVE"
                                )
                                if provider == "resend"
                                else "SYNTHETIC_FIXTURE",
                                "source_provider": provider,
                                "sender_authority": "UNVERIFIED",
                                "provider_attachments": [
                                    item.model_dump(mode="json") for item in result.value.attachments
                                ],
                                "provider_source": result.value.source.model_dump(
                                    mode="json", exclude={"content"}
                                ),
                            },
                        )
                    )
        async with sessions.begin() as db:
            row = await db.run_sync(lambda store: current_receipt(store, ctx))
            saved = await db.get(Communication, communication_id)
            assert saved is not None
            communication = saved
            if commands:
                # Source success means original bytes stored; quarantined media remains visibly unavailable.
                from aco.features.artifacts.models import Artifact

                original = await db.get(Artifact, commands[0].artifact_id)
                communication.source_state = (
                    "stored" if original and original.storage_state == "stored" else "quarantined"
                )
            elif communication.direction == "incoming":
                communication.source_state = "unavailable"
            links = tuple(
                await db.scalars(
                    select(CommunicationInvoiceLink.invoice_id).where(
                        CommunicationInvoiceLink.tenant_id == ctx.tenant_id,
                        CommunicationInvoiceLink.communication_id == communication_id,
                    )
                )
            )
            prior_run = await db.scalar(
                select(EmailIngressReceipt.agent_run_id)
                .where(
                    EmailIngressReceipt.tenant_id == ctx.tenant_id,
                    EmailIngressReceipt.communication_id == communication_id,
                    EmailIngressReceipt.agent_run_id.is_not(None),
                )
                .limit(1)
            )
            row.agent_run_id = prior_run
            relevant = (
                communication.direction == "incoming"
                and communication.kind in {"MESSAGE", "CUSTOMER_REPLY"}
                and communication.source_state == "stored"
                and 1 <= len(set(links)) <= 3
            )
            if relevant and producer is not None and prior_run is None:
                activation = await db.run_sync(
                    lambda store: current_activation(store, ctx.activation_id, ctx.tenant_id)
                )
                source = InboundAgentSource(
                    receipt_id=row.id,
                    communication_id=communication_id,
                    activation_id=activation.id,
                    tenant_id=ctx.tenant_id,
                    enabling_member_id=activation.enabling_member_id,
                    membership_version=activation.membership_version,
                    worker_actor_id=activation.worker_actor_id,
                    invoice_ids=tuple(sorted(set(links), key=str)),
                    correlation_id=ctx.correlation_id,
                )
                await db.flush()
                row.agent_run_id = await db.run_sync(lambda store: producer(store, ctx, source))
            if provider == "resend":
                from aco.features.communications.provider_ingress import record_capture_outcome

                await db.run_sync(lambda store: record_capture_outcome(store, ctx, "COMMITTED"))
            row.state = "processed" if row.agent_run_id or not relevant else "review_required"
            row.lease_token, row.lease_until = None, None
    except Exception as error:
        async with sessions.begin() as db:
            row = await db.get(EmailIngressReceipt, receipt_id, with_for_update=True)
            if row is not None and row.lease_token == ctx.lease_token:
                if provider == "resend":
                    from aco.features.communications.provider_ingress import record_capture_outcome

                    await db.run_sync(lambda store: record_capture_outcome(store, ctx, "FAILED"))
                row.state, row.error_code = (
                    "failed",
                    str(error.detail)
                    if isinstance(error, HTTPException)
                    else "INGRESS_FETCH_OR_CAPTURE_FAILED",
                )
                row.lease_token, row.lease_until = None, None
        raise
