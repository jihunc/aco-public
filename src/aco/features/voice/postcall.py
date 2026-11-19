"""One post-call record and permitted private originals; never replay committed call tools."""

import json
from functools import partial
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid5

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.features.accounting.models import InvoiceSnapshot
from aco.features.artifacts.consent import consent_command, consent_reasons
from aco.features.artifacts.consent_models import ConsentEvidence, VoicePostCall
from aco.features.artifacts.dto import ArtifactWrite
from aco.features.artifacts.models import Artifact
from aco.features.communications.models import (
    Communication,
    CommunicationInvoiceLink,
    CommunicationSource,
    CommunicationThread,
)
from aco.features.identity.models import Tenant
from aco.features.identity.service import IdentityService, Permission
from aco.features.review.models import ApprovalRequest
from aco.features.voice.dto import PrepareVoice, VoiceApprovalProposal, VoiceSessionView
from aco.features.voice.models import VoiceEventRecord, VoiceSession
from aco.features.voice.service import VoiceService, source_links
from aco.integrations.ports.voice_artifacts import SyntheticTranscript, VoiceArtifactRequest

if TYPE_CHECKING:
    from aco.features.artifacts.service import ArtifactService
    from aco.features.voice.runtime import VoiceRuntime


def completed(db: Session, ctx: TenantContext, key: UUID) -> VoiceSession:
    if not isinstance(ctx, TenantContext):
        raise HTTPException(403, "VOICE_POSTCALL_HUMAN_REQUIRED")
    IdentityService(db).authorize(ctx, Permission.INVOICE_READ)
    row = db.get(VoiceSession, key, populate_existing=True)
    if row is None or row.tenant_id != ctx.tenant_id:
        raise HTTPException(404, "VOICE_SESSION_NOT_FOUND")
    if row.state != "completed" or row.provider_call_id is None:
        raise HTTPException(409, "VOICE_POSTCALL_NOT_COMPLETED")
    approval = db.get(ApprovalRequest, row.approval_id)
    if (
        approval is None
        or approval.tenant_id != row.tenant_id
        or approval.status != "approved"
        or approval.consumed_at is None
    ):
        raise HTTPException(403, "VOICE_POSTCALL_APPROVAL_REQUIRED")
    return row


def capture_enabled(runtime: "VoiceRuntime") -> None:
    if runtime.capture_port is None or not runtime.enabled or runtime.environment != "local":
        raise HTTPException(403, "VOICE_CAPTURE_PROVIDER_UNAVAILABLE")


def permitted(
    db: Session, ctx: TenantContext, request: VoiceArtifactRequest, runtime: "VoiceRuntime | None" = None
) -> Tenant:
    if runtime is not None:
        capture_enabled(runtime)
    tenant = IdentityService(db).authorize(ctx, Permission.SETTINGS)
    row = completed(db, ctx, request.session_id)
    if (
        runtime is not None
        and runtime.binding_hash(runtime.binding(row.tenant_id, row.connection_id)) != row.binding_hash
    ):
        raise HTTPException(403, "VOICE_CONFIGURATION_CHANGED")
    command = PrepareVoice.model_validate_json(json.dumps(row.command))
    consent = db.get(ConsentEvidence, request.consent_id, populate_existing=True)
    if (
        not tenant.is_demo
        or request.tenant_id != ctx.tenant_id
        or consent is None
        or consent.tenant_id != ctx.tenant_id
        or command.consent_id != consent.id
        or request.connection_id != row.connection_id
        or consent.voice_connection_id != row.connection_id
        or request.provider_call_id != row.provider_call_id
    ):
        raise HTTPException(403, "VOICE_CAPTURE_SCOPE_DENIED")
    reasons = consent_reasons(db, consent)
    if reasons:
        raise HTTPException(403, reasons[0])
    approval = db.get(ApprovalRequest, row.approval_id)
    assert approval
    proposal = VoiceApprovalProposal.model_validate_json(json.dumps(approval.proposal))
    scopes = consent_command(consent).scopes
    if (
        proposal.consent_id != consent.id
        or proposal.consent_hash != consent.material_hash
        or proposal.capture_scopes != scopes
        or proposal.capture != "SYNTHETIC_ARTIFACTS"
        or not scopes.private_storage
        or not scopes.provider_retention
        or not (scopes.transcript if request.kind == "transcript" else scopes.audio_capture)
    ):
        raise HTTPException(403, "VOICE_CAPTURE_NOT_PERMITTED")
    links = source_links(db, row)
    if {link.invoice_id for link in links} != set(command.invoice_ids):
        raise HTTPException(403, "VOICE_CAPTURE_SCOPE_DENIED")
    for link in links:
        invoice = db.get(InvoiceSnapshot, link.invoice_id)
        if (
            invoice is None
            or invoice.tenant_id != row.tenant_id
            or invoice.customer_id != consent.customer_id
        ):
            raise HTTPException(403, "VOICE_CAPTURE_SCOPE_DENIED")
    event = db.scalar(
        select(VoiceEventRecord)
        .where(
            VoiceEventRecord.tenant_id == row.tenant_id,
            VoiceEventRecord.session_id == row.id,
            VoiceEventRecord.kind == "call.completed",
        )
        .order_by(VoiceEventRecord.created_at)
        .limit(1)
    )
    if event is None or event.created_at != request.captured_at or event.created_at >= consent.expires_at:
        raise HTTPException(403, "VOICE_CAPTURE_EVENT_REQUIRED")
    artifact = db.get(Artifact, uuid5(row.id, "voice-private:" + request.kind), populate_existing=True)
    if artifact and (
        artifact.revoked_at
        or artifact.consent_revoked
        or artifact.legal_hold_state != "none"
        or artifact.storage_state in {"deleted", "purge_pending", "blocked"}
    ):
        raise HTTPException(410, "VOICE_CAPTURE_REVOKED")
    return tenant


def communication(db: Session, ctx: TenantContext, key: UUID) -> VoicePostCall:
    row = completed(db, ctx, key)
    old = db.scalar(
        select(VoicePostCall).where(VoicePostCall.tenant_id == ctx.tenant_id, VoicePostCall.session_id == key)
    )
    if old:
        return old
    IdentityService(db).authorize(ctx, Permission.INVESTIGATE)
    command = PrepareVoice.model_validate_json(json.dumps(row.command))
    invoice = db.get(InvoiceSnapshot, command.invoice_ids[0])
    assert invoice and invoice.tenant_id == ctx.tenant_id
    thread = CommunicationThread(
        tenant_id=ctx.tenant_id,
        provider_connection_id=row.connection_id,
        origin_invoice_id=invoice.id,
        customer_id=invoice.customer_id,
        purpose="voice_native",
        native_thread_id="call:" + str(row.provider_call_id),
    )
    db.add(thread)
    db.flush()
    event = db.scalar(
        select(VoiceEventRecord)
        .where(VoiceEventRecord.session_id == row.id, VoiceEventRecord.kind == "call.completed")
        .order_by(VoiceEventRecord.created_at)
        .limit(1)
    )
    if event is None:
        raise HTTPException(409, "VOICE_POSTCALL_SOURCE_EVENT_REQUIRED")
    call = Communication(
        tenant_id=ctx.tenant_id,
        provider_connection_id=row.connection_id,
        thread_id=thread.id,
        origin_invoice_id=invoice.id,
        operation_id=row.id,
        native_message_id="call:" + str(row.provider_call_id),
        message_sha256=event.raw_sha256,
        direction="outgoing",
        channel="voice",
        kind="CALL",
        synthetic=True,
        content={
            "voice_session_id": str(row.id),
            "evidence_mode": "MOCKED_HTTP",
            "outcome": "completed",
            "post_call_action_policy": "committed_tools_are_not_repeated",
        },
        event_at=event.created_at,
        delivery_state="completed",
        source_state="metadata_only",
    )
    db.add(call)
    db.flush()
    for link in source_links(db, row):
        db.add(
            CommunicationInvoiceLink(
                tenant_id=ctx.tenant_id,
                communication_id=call.id,
                invoice_id=link.invoice_id,
                claim_or_context_key="voice_scope:" + str(link.invoice_id),
                provenance="origin_thread",
                confidence="session_approved_scope",
                evidence_excerpt="",
                evidence_offset=0,
                classification="synthetic_voice_context",
            )
        )
    result = VoicePostCall(tenant_id=ctx.tenant_id, session_id=row.id, communication_id=call.id)
    db.add(result)
    db.flush()
    IdentityService(db).audit(
        ctx, "voice.post_call_reconciled", row.id, {"communication_id": str(call.id), "synthetic": True}
    )
    return result


async def reconcile(
    runtime: "VoiceRuntime", artifacts: "ArtifactService | None", ctx: TenantContext, key: UUID
) -> VoiceSessionView:
    async with runtime.sessions.begin() as db:
        post = await db.run_sync(lambda store: communication(store, ctx, key))
        row = await db.get(VoiceSession, key)
        assert row
        command = PrepareVoice.model_validate_json(json.dumps(row.command))
        consent = await db.get(ConsentEvidence, command.consent_id) if command.consent_id else None
        kinds: list[Literal["transcript", "recording"]] = []
        if consent:
            scopes = consent_command(consent).scopes
            if scopes.transcript:
                kinds.append("transcript")
            if scopes.audio_capture:
                kinds.append("recording")
        event = await db.scalar(
            select(VoiceEventRecord)
            .where(VoiceEventRecord.session_id == key, VoiceEventRecord.kind == "call.completed")
            .order_by(VoiceEventRecord.created_at)
            .limit(1)
        )
        assert event and row.provider_call_id
        requests = (
            [
                VoiceArtifactRequest(
                    tenant_id=ctx.tenant_id,
                    connection_id=row.connection_id,
                    session_id=key,
                    consent_id=consent.id,
                    provider_call_id=row.provider_call_id,
                    captured_at=event.created_at,
                    kind=kind,
                )
                for kind in kinds
            ]
            if consent
            else []
        )
    for request in requests:
        if artifacts is None:
            raise HTTPException(503, "STORAGE_NOT_CONFIGURED")
        capture_enabled(runtime)
        assert runtime.capture_port is not None

        async def authorize(received: VoiceArtifactRequest, expected: VoiceArtifactRequest = request) -> None:
            if received != expected:
                raise HTTPException(403, "VOICE_CAPTURE_SCOPE_DENIED")
            async with runtime.sessions.begin() as db:
                await db.run_sync(lambda store: permitted(store, ctx, received, runtime))

        original = await runtime.capture_port.fetch(request, authorize)
        await authorize(request)
        allowed_types = {"application/json"} if request.kind == "transcript" else {"audio/wav", "audio/mpeg"}
        if (
            original.request != request
            or original.kind != request.kind
            or original.captured_at != request.captured_at
            or original.content_type not in allowed_types
            or not original.reference.strip()
        ):
            raise HTTPException(403, "VOICE_CAPTURE_RESULT_SCOPE_DENIED")
        if request.kind == "transcript":
            try:
                SyntheticTranscript.model_validate_json(original.body)
            except ValidationError:
                raise HTTPException(403, "VOICE_TRANSCRIPT_SHAPE_INVALID") from None
        value = ArtifactWrite(
            artifact_id=uuid5(key, "voice-private:" + request.kind),
            source_kind="provider_call",
            source_reference="synthetic-call:" + str(key) + ":" + request.kind,
            content_type=original.content_type,
            body=original.body,
            capture_class="synthetic_consented_voice",
            captured_at=original.captured_at,
        )
        await artifacts.store_voice_artifact(ctx, request, value, original.reference, runtime)
        async with runtime.sessions.begin() as db:
            await db.run_sync(partial(permitted, ctx=ctx, request=request, runtime=runtime))
            artifact = await db.get(Artifact, value.artifact_id)
            call = await db.get(Communication, post.communication_id)
            assert artifact and call
            old = await db.scalar(
                select(CommunicationSource).where(
                    CommunicationSource.tenant_id == ctx.tenant_id,
                    CommunicationSource.communication_id == call.id,
                    CommunicationSource.artifact_id == artifact.id,
                )
            )
            if old is None:
                db.add(
                    CommunicationSource(
                        tenant_id=ctx.tenant_id,
                        communication_id=call.id,
                        artifact_id=artifact.id,
                        representation="voice_" + request.kind,
                        sha256=artifact.sha256,
                        evidence={
                            "consent_id": str(request.consent_id),
                            "session_id": str(key),
                            "synthetic": True,
                            "evidence_mode": "FAKE",
                        },
                    )
                )
            call.source_state = "private_original"
    async with runtime.sessions.begin() as db:
        return await db.run_sync(lambda store: VoiceService(store, runtime).get(ctx, key))
