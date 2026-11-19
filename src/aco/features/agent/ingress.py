"""Receipt-bound fake investigations; source references never confer authority."""

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.communications.models import Communication, CommunicationInvoiceLink

if TYPE_CHECKING:
    from aco.features.agent.models import AgentRun
    from aco.features.communications.ingress_dto import InboundAgentSource
    from aco.features.communications.models import EmailIngressActivation, EmailIngressReceipt
    from aco.integrations.ports.email_context import EmailIngressContext


def bounded_linked_scope(db: Session, message: Communication) -> tuple[UUID, ...] | None:
    """Retain the complete persisted scope or require review; never select a subset."""
    links = list(
        db.scalars(
            select(CommunicationInvoiceLink).where(
                CommunicationInvoiceLink.tenant_id == message.tenant_id,
                CommunicationInvoiceLink.communication_id == message.id,
            )
        )
    )
    keys = {link.invoice_id for link in links}
    if not 1 <= len(keys) <= 3 or any(
        link.confidence not in {"1", "contextual_unverified_sender"}
        or (
            link.confidence == "contextual_unverified_sender"
            and link.provenance not in {"origin_thread", "explicit_reference", "contextual_inference"}
        )
        for link in links
    ):
        return None
    invoices = list(
        db.scalars(
            select(InvoiceSnapshot).where(
                InvoiceSnapshot.tenant_id == message.tenant_id,
                InvoiceSnapshot.id.in_(keys),
            )
        )
    )
    if (
        len(invoices) != len(keys)
        or any(not row.verified for row in invoices)
        or len({row.customer_id for row in invoices}) != 1
    ):
        return None
    return tuple(sorted(keys))


def produce_inbound_run(db: Session, ctx: "EmailIngressContext", source: "InboundAgentSource") -> UUID | None:
    """Called only inside the actual maintained ingress job transaction, never an HTTP grant."""
    from fastapi import HTTPException

    from aco.core.context import TenantContext

    if isinstance(ctx, TenantContext):
        raise HTTPException(403, "INGRESS_JOB_AUTHORITY_REQUIRED")
    from uuid import uuid4

    from aco.features.agent.context import selected_invoices
    from aco.features.agent.events import append_event
    from aco.features.agent.models import AgentInvoice, AgentRun
    from aco.features.communications.ingress import current_activation, current_receipt
    from aco.features.communications.ingress_dto import InboundAgentSource
    from aco.features.communications.models import EmailIngressActivation, EmailIngressReceipt
    from aco.features.identity.models import Actor, Membership, ProviderConnection, Tenant
    from aco.features.identity.service import IdentityService
    from aco.integrations.ports.email_context import EmailIngressContext

    if not isinstance(ctx, EmailIngressContext) or not isinstance(source, InboundAgentSource):
        raise HTTPException(403, "INGRESS_JOB_AUTHORITY_REQUIRED")
    db.scalar(select(Tenant).where(Tenant.id == ctx.tenant_id).with_for_update())
    activation = db.scalar(
        select(EmailIngressActivation)
        .where(
            EmailIngressActivation.id == ctx.activation_id,
            EmailIngressActivation.tenant_id == ctx.tenant_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if activation is None:
        raise HTTPException(403, "INGRESS_ACTIVATION_INVALID")
    # These are current persisted capabilities, not copies carried in a caller DTO.
    db.scalar(
        select(Membership)
        .where(Membership.tenant_id == ctx.tenant_id, Membership.user_id == activation.enabling_member_id)
        .with_for_update(key_share=True)
    )
    db.scalar(select(Actor).where(Actor.id == activation.worker_actor_id).with_for_update(key_share=True))
    list(
        db.scalars(
            select(ProviderConnection)
            .where(
                ProviderConnection.id.in_([activation.email_connection_id, activation.storage_connection_id])
            )
            .order_by(ProviderConnection.id)
            .with_for_update(key_share=True)
        )
    )
    db.scalar(
        select(EmailIngressReceipt)
        .where(EmailIngressReceipt.id == ctx.receipt_id, EmailIngressReceipt.tenant_id == ctx.tenant_id)
        .with_for_update()
    )
    receipt = current_receipt(db, ctx)
    activation = current_activation(db, ctx.activation_id, ctx.tenant_id)
    if (
        source.mode != "FAKE"
        or source.tenant_id != ctx.tenant_id
        or source.receipt_id != receipt.id
        or source.activation_id != activation.id
        or source.communication_id != receipt.communication_id
        or source.enabling_member_id != activation.enabling_member_id
        or source.membership_version != activation.membership_version
        or source.worker_actor_id != activation.worker_actor_id
        or source.correlation_id != ctx.correlation_id
    ):
        raise HTTPException(403, "INGRESS_SOURCE_REFERENCE_MISMATCH")
    message = captured_source(db, receipt, activation)
    scope = bounded_linked_scope(db, message)
    if scope is None:
        if receipt.error_code != "INBOUND_SCOPE_REVIEW_REQUIRED":
            IdentityService(db).audit(
                ctx,
                "agent.inbound_review_required",
                receipt.id,
                {"communication_id": str(message.id), "synthetic": True},
            )
        receipt.review_required, receipt.error_code = True, "INBOUND_SCOPE_REVIEW_REQUIRED"
        return None
    if len(source.invoice_ids) != len(scope) or set(source.invoice_ids) != set(scope):
        raise HTTPException(403, "INGRESS_SOURCE_SCOPE_MISMATCH")
    old = db.scalar(
        select(AgentRun).where(AgentRun.tenant_id == ctx.tenant_id, AgentRun.source_receipt_id == receipt.id)
    )
    if old is not None:
        if (
            old.source_communication_id != message.id
            or old.source_activation_id != activation.id
            or old.initiator_id != activation.enabling_member_id
            or old.worker_actor_id != ctx.actor_id
            or old.authenticity != "FAKE"
            or set(selected_invoices(db, old)) != set(scope)
        ):
            raise HTTPException(409, "INGRESS_RUN_REPLAY_CONFLICT")
        receipt.agent_run_id = old.id
        return old.id
    connection = db.scalar(
        select(ProviderConnection)
        .where(
            ProviderConnection.tenant_id == ctx.tenant_id,
            ProviderConnection.provider_type == "llm",
            ProviderConnection.provider == "mock",
        )
        .order_by(ProviderConnection.created_at.desc(), ProviderConnection.id)
    )
    if connection is None:
        connection = ProviderConnection(
            tenant_id=ctx.tenant_id,
            provider_type="llm",
            provider="mock",
            status="READY",
            capabilities=["structured_output", "tool_calling"],
        )
        db.add(connection)
        db.flush()
    if connection.status != "READY":
        receipt.review_required, receipt.error_code = True, "LLM_CONNECTION_UNAVAILABLE"
        IdentityService(db).audit(
            ctx,
            "agent.inbound_review_required",
            receipt.id,
            {"communication_id": str(message.id), "reason": "LLM_CONNECTION_UNAVAILABLE"},
        )
        return None
    key = uuid4()
    run = AgentRun(
        id=key,
        tenant_id=ctx.tenant_id,
        request_id=uuid4(),
        initiator_id=activation.enabling_member_id,
        membership_version=activation.membership_version,
        worker_actor_id=activation.worker_actor_id,
        actor_version=activation.actor_version,
        tenant_generation=activation.tenant_generation,
        provider_connection_id=connection.id,
        connection_version=connection.version,
        correlation_id=ctx.correlation_id,
        objective=f"Review captured inbound communication {message.id} "
        "and create an internal intake review task. "
        "Sender authority remains unverified; no outreach or financial changes.",
        thread_id=f"{ctx.tenant_id}:{key}",
        authenticity="FAKE",
        source_receipt_id=receipt.id,
        source_communication_id=message.id,
        source_activation_id=activation.id,
    )
    db.add(run)
    db.flush()
    for invoice_id in scope:
        db.add(AgentInvoice(tenant_id=ctx.tenant_id, run_id=run.id, invoice_id=invoice_id))
    receipt.agent_run_id = run.id
    append_event(db, run, "queued", f"FAKE inbound investigation queued from receipt {receipt.id}")
    IdentityService(db).audit(
        ctx,
        "agent.inbound_queued",
        run.id,
        {
            "receipt_id": str(receipt.id),
            "communication_id": str(message.id),
            "activation_id": str(activation.id),
            "enabling_member_id": str(activation.enabling_member_id),
            "synthetic": True,
        },
    )
    return run.id


def captured_source(
    db: Session, receipt: "EmailIngressReceipt", activation: "EmailIngressActivation"
) -> Communication:
    from fastapi import HTTPException

    from aco.features.artifacts.models import Artifact
    from aco.features.communications.models import CommunicationSource, EmailIngressReceipt
    from aco.features.identity.models import now

    message = db.scalar(
        select(Communication)
        .where(Communication.id == receipt.communication_id, Communication.tenant_id == receipt.tenant_id)
        .execution_options(populate_existing=True)
    )
    if (
        message is None
        or message.direction != "incoming"
        or message.channel != "email"
        or message.kind not in {"MESSAGE", "CUSTOMER_REPLY"}
        or not message.synthetic
        or message.source_state != "stored"
        or message.provider_connection_id != activation.email_connection_id
        or message.native_message_id != receipt.message_id
    ):
        raise HTTPException(403, "INGRESS_CAPTURED_SOURCE_REQUIRED")
    originals = db.execute(
        select(CommunicationSource, Artifact)
        .join(
            Artifact,
            (Artifact.id == CommunicationSource.artifact_id)
            & (Artifact.tenant_id == CommunicationSource.tenant_id),
        )
        .where(
            CommunicationSource.tenant_id == receipt.tenant_id,
            CommunicationSource.communication_id == message.id,
            CommunicationSource.representation == "provider_original",
        )
    ).all()
    # The immutable original retains its archival receipt across later notifications.
    # That receipt proves provenance only; current_receipt/current_activation grant work.
    archival_receipts = {
        str(key)
        for key in db.scalars(
            select(EmailIngressReceipt.id).where(
                EmailIngressReceipt.tenant_id == receipt.tenant_id,
                EmailIngressReceipt.email_connection_id == activation.email_connection_id,
                EmailIngressReceipt.message_id == receipt.message_id,
                EmailIngressReceipt.communication_id == message.id,
            )
        )
    }
    if not any(
        src.evidence.get("receipt_id") in archival_receipts
        and src.evidence.get("synthetic") is True
        and src.evidence.get("sender_authority") == "UNVERIFIED"
        and artifact.provider_connection_id == activation.storage_connection_id
        and artifact.storage_state == "stored"
        and artifact.sha256 == src.sha256
        and artifact.expires_at > now()
        and artifact.revoked_at is None
        and not artifact.consent_revoked
        and artifact.deleted_at is None
        for src, artifact in originals
    ):
        raise HTTPException(403, "INGRESS_CAPTURED_SOURCE_REQUIRED")
    return message


def validate_inbound_run(db: Session, run: "AgentRun") -> None:
    """Fresh source/activation authority for a later agent job, not the completed ingress lease."""
    from fastapi import HTTPException

    from aco.features.agent.context import selected_invoices
    from aco.features.communications.ingress import current_activation
    from aco.features.communications.models import EmailIngressReceipt

    if run.source_activation_id is None or run.source_receipt_id is None:
        raise HTTPException(403, "INGRESS_RUN_AUTHORITY_REVOKED")
    activation = current_activation(db, run.source_activation_id, run.tenant_id)
    receipt = db.get(EmailIngressReceipt, run.source_receipt_id, populate_existing=True)
    if (
        receipt is None
        or receipt.tenant_id != run.tenant_id
        or receipt.activation_id != activation.id
        or receipt.state != "processed"
        or receipt.agent_run_id != run.id
        or receipt.communication_id != run.source_communication_id
        or run.authenticity != "FAKE"
        or run.initiator_id != activation.enabling_member_id
        or run.membership_version != activation.membership_version
        or run.worker_actor_id != activation.worker_actor_id
        or run.correlation_id != receipt.correlation_id
    ):
        raise HTTPException(403, "INGRESS_RUN_AUTHORITY_REVOKED")
    message = captured_source(db, receipt, activation)
    scope = bounded_linked_scope(db, message)
    if scope is None or set(scope) != set(selected_invoices(db, run)):
        raise HTTPException(403, "INGRESS_RUN_SCOPE_CHANGED")
