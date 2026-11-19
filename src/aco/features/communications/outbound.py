"""Deterministic invoice email with committed preparation, private original and independent intent."""

import hashlib
import json
from datetime import timedelta
from email.message import EmailMessage as MimeMessage
from email.policy import SMTP
from uuid import UUID, uuid5

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from aco.core.context import ActorContext, ServiceContext
from aco.core.settings import Settings
from aco.core.types import ActionResult, ActionStatus, ProviderErrorCode, ProviderFailure, ProviderReference
from aco.features.accounting.models import InvoiceSnapshot
from aco.features.actions.models import ActionExecution, ScheduledAction
from aco.features.actions.service import ActionFailure, ActionLease, ActionService
from aco.features.artifacts.dto import ArtifactWrite
from aco.features.artifacts.models import Artifact
from aco.features.artifacts.service import ArtifactService
from aco.features.communications.activation import latest, system_enabled, validate_activation
from aco.features.communications.capture import capture_invocation
from aco.features.communications.dto import ApprovedOutboundEmail
from aco.features.communications.models import (
    Communication,
    CommunicationCaptureGrant,
    CommunicationDeliveryEvent,
    CommunicationInvoiceLink,
    CommunicationSource,
    CommunicationThread,
    EmailDispatchBinding,
    EmailExecutionActivation,
    EmailSubmission,
)
from aco.features.communications.provider_outbound import ProviderOutboundRuntime
from aco.features.communications.reminder_text import reminder_body
from aco.features.identity.models import Actor, Tenant, now
from aco.features.identity.service import IdentityService, Permission
from aco.features.policies.dto import DispatchTicket, OutboundProposal, PolicyDefinition
from aco.features.policies.guards import (
    PROCESS_INCARNATION,
    PolicyGuards,
    normalized_destination,
    read_dispatch_source,
)
from aco.features.policies.models import CollectionPolicyVersion, ContactReservation, PolicyOccurrence
from aco.features.policies.service import PolicyService
from aco.features.usage.email import ObservedEmail
from aco.integrations.demo_accounting.adapter import DemoAccountingAdapter
from aco.integrations.demo_email.adapter import DemoEmailRuntime
from aco.integrations.ports.email import EmailFailure
from aco.integrations.ports.email_dto import EmailContent, OutboundEmail
from aco.integrations.ports.storage import StorageFailure, StorageRuntime

# Stable UUIDv5 namespace; retries use tenant + full immutable action operation key.
EMAIL_OPERATION_NAMESPACE = UUID("2b2bf02d-13b0-5b33-9a99-b0e9eb119aa8")


def payload_hash(command: OutboundEmail) -> str:
    return hashlib.sha256(command.model_dump_json().encode()).hexdigest()


def compose_original(command: OutboundEmail, boundary: str) -> bytes:
    message = MimeMessage(policy=SMTP)
    message["From"] = command.sender
    message["To"] = ", ".join(command.to)
    if command.cc:
        message["Cc"] = ", ".join(command.cc)
    message["Subject"] = command.subject
    message["Message-ID"] = command.message_id
    if command.in_reply_to:
        message["In-Reply-To"] = command.in_reply_to
    if command.references:
        message["References"] = " ".join(command.references)
    message.set_content(command.text_body or "")
    if command.html_body is not None:
        message.add_alternative(command.html_body, subtype="html")
        message.set_boundary(boundary)
    # T10 deterministic templates have no attachment-editing surface. Future producers must
    # resolve every referenced artifact through read_ingested_attachment before composing.
    if command.attachments:
        raise HTTPException(409, "BOUND_ATTACHMENT_COMPOSITION_REQUIRED")
    return message.as_bytes()


def dispatch_prerequisites(
    db: Session,
    ctx: ActorContext,
    proposal: OutboundProposal,
    settings: Settings | None,
    runtime: object,
    *,
    pending_attempt: UUID | None = None,
) -> list[str]:
    if settings is None or not system_enabled(settings, runtime):
        return ["GLOBAL_OUTBOUND_DISABLED", "OUTBOUND_EXECUTOR_UNAVAILABLE"]
    action = db.get(ScheduledAction, proposal.action_id, populate_existing=True)
    if action is None or action.tenant_id != ctx.tenant_id:
        return ["PERSISTED_EMAIL_ACTION_REQUIRED"]
    if action.kind == "agent_email":
        from aco.features.review.email_approvals import current_email_approval

        try:
            from aco.features.policies.models import DispatchTicketRecord

            consumed = db.scalar(
                select(DispatchTicketRecord).where(
                    DispatchTicketRecord.tenant_id == ctx.tenant_id,
                    DispatchTicketRecord.action_id == action.id,
                    DispatchTicketRecord.attempt_id == proposal.attempt_id,
                    DispatchTicketRecord.read_id == proposal.read_id,
                    DispatchTicketRecord.incarnation == PROCESS_INCARNATION,
                    DispatchTicketRecord.consumed_at.is_not(None),
                )
            )
            reviewed = current_email_approval(db, action, allow_consumed=consumed is not None)
            if reviewed.consumed_at is not None and (
                consumed is None
                or consumed.consumed_at != reviewed.consumed_at
                or consumed.bindings.get("approval") != str(reviewed.id)
            ):
                return ["EMAIL_APPROVAL_CONSUMPTION_OUTSIDE_ATTEMPT"]
            if reviewed.id != proposal.approval_id:
                return ["EMAIL_APPROVAL_ACTION_MISMATCH"]
        except HTTPException as error:
            return [str(error.detail)]
    elif proposal.approval_id is not None:
        return ["AI_APPROVAL_EXECUTOR_UNAVAILABLE"]
    binding = db.scalar(
        select(EmailDispatchBinding).where(
            EmailDispatchBinding.tenant_id == ctx.tenant_id,
            EmailDispatchBinding.action_id == proposal.action_id,
        )
    )
    tenant = db.get(Tenant, ctx.tenant_id, populate_existing=True)
    if binding is None or tenant is None:
        return ["PERSISTED_EMAIL_BINDING_REQUIRED"]
    activation = db.get(EmailExecutionActivation, binding.activation_id)
    try:
        if activation is None:
            raise HTTPException(403, "EMAIL_EXECUTION_REVIEW_REQUIRED")
        validate_activation(db, tenant, activation)
        if isinstance(runtime, ProviderOutboundRuntime):
            runtime.validate(db, activation)
    except HTTPException as error:
        return [str(error.detail)]
    command = OutboundEmail.model_validate_json(json.dumps(binding.command))
    if (
        payload_hash(command) != binding.payload_hash
        or command.operation_id != binding.operation_id
        or command.provider_connection_id != activation.email_connection_id
        or command.sender != activation.sender
        or any(
            normalized_destination(v) not in activation.destinations
            for v in (*command.to, *command.cc, *command.bcc)
        )
        or tuple(command.to) != (proposal.destination,)
        or proposal.channel != "email"
    ):
        return ["EMAIL_MATERIAL_CHANGED"]
    submission = db.scalar(
        select(EmailSubmission)
        .where(EmailSubmission.tenant_id == ctx.tenant_id, EmailSubmission.binding_id == binding.id)
        .execution_options(populate_existing=True)
    )
    if submission is None or (
        submission.state not in {"prepared", "no_effect"}
        and not (
            pending_attempt is not None
            and submission.state == "pending"
            and submission.usage_attempt_id == pending_attempt
        )
    ):
        return ["EMAIL_OPERATION_FENCED"]
    source = db.scalar(
        select(CommunicationSource).where(
            CommunicationSource.tenant_id == ctx.tenant_id,
            CommunicationSource.communication_id == binding.communication_id,
            CommunicationSource.sha256 == binding.mime_sha256,
        )
    )
    artifact = db.get(Artifact, source.artifact_id, populate_existing=True) if source else None
    if (
        artifact is None
        or artifact.storage_state != "stored"
        or artifact.revoked_at
        or artifact.consent_revoked
        or artifact.expires_at <= now()
        or artifact.legal_hold_state != "none"
    ):
        return ["ORIGINAL_CAPTURE_REQUIRED"]
    contact = db.get(ContactReservation, binding.contact_reservation_id, populate_existing=True)
    if (
        contact is None
        or contact.tenant_id != ctx.tenant_id
        or contact.action_id != proposal.action_id
        or contact.operation_id != binding.operation_id
        or contact.status != "reserved"
    ):
        return ["CONTACT_RESERVATION_INVALID"]
    return []


class ReminderDispatcher:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        email_runtime: DemoEmailRuntime | ProviderOutboundRuntime,
        storage_runtime: StorageRuntime,
    ):
        self.sessions, self.settings, self.email_runtime = sessions, settings, email_runtime
        self.artifacts = ArtifactService(sessions, storage_runtime, settings.app_env)
        self.incarnation = PROCESS_INCARNATION

    def guard(self, db: Session) -> PolicyGuards:
        return PolicyGuards(
            db, now(), self.incarnation, settings=self.settings, email_runtime=self.email_runtime
        )

    async def prepare(self, ctx: ServiceContext, action_id: UUID) -> tuple[UUID, UUID]:
        async with self.sessions.begin() as db:
            tenant = await db.run_sync(
                lambda store: IdentityService(store).authorize(ctx, Permission.INVOICE_READ)
            )
            action = await db.get(ScheduledAction, action_id)
            if (
                action is None
                or action.id != ctx.action_id
                or action.kind not in {"policy_due", "agent_email"}
                or action.status != "executing"
                or action.command.get("channel") != "email"
            ):
                raise HTTPException(403, "ACTIVE_EMAIL_ACTION_REQUIRED")
            if not system_enabled(self.settings, self.email_runtime):
                raise HTTPException(409, "OUTBOUND_EXECUTOR_UNAVAILABLE")
            activation = await db.run_sync(lambda store: latest(store, ctx.tenant_id))
            if activation is None:
                raise HTTPException(403, "EMAIL_EXECUTION_REVIEW_REQUIRED")
            await db.run_sync(lambda store: validate_activation(store, tenant, activation))
            provider_runtime = self.email_runtime
            if isinstance(provider_runtime, ProviderOutboundRuntime):
                await db.run_sync(lambda store: provider_runtime.validate(store, activation))
            attempt = await db.scalar(
                select(ActionExecution).where(
                    ActionExecution.action_id == action.id, ActionExecution.lease_token == ctx.lease_token
                )
            )
            assert attempt
            read = await read_dispatch_source(
                db,
                ctx,
                action.invoice_id,
                attempt.id,
                self.incarnation,
                DemoAccountingAdapter(db, action.provider_connection_id),
            )
            invoice = await db.get(InvoiceSnapshot, action.invoice_id, populate_existing=True)
            assert invoice
            if invoice.source_revision != action.source_revision:
                # Retain the real verified import; old immutable command never gains new authority.
                return action.id, UUID(int=0)
            source = await db.run_sync(
                lambda store: self.guard(store).source_status(ctx, invoice.id, attempt.id, read)
            )
            if not source.allowed:
                raise HTTPException(409, source.reason)
            binding = await db.scalar(
                select(EmailDispatchBinding).where(
                    EmailDispatchBinding.tenant_id == ctx.tenant_id,
                    EmailDispatchBinding.action_id == action.id,
                )
            )
            if binding:
                submission = await db.scalar(
                    select(EmailSubmission).where(EmailSubmission.binding_id == binding.id)
                )
                if submission is None or submission.state not in {"prepared", "no_effect"}:
                    raise HTTPException(409, "EMAIL_OPERATION_FENCED")
            else:

                def build(store: Session) -> EmailDispatchBinding:
                    service = PolicyService(store, now())
                    version = store.get(CollectionPolicyVersion, action.policy_version_id)
                    assert version
                    policy = service.policy(ctx, version.policy_id)
                    preview = service.preview_policy(ctx, service.view(policy, version), now())
                    matching = next(
                        (
                            i
                            for i in preview.items
                            if i.invoice_id == action.invoice_id
                            and str(i.rule_id) == action.command["rule_id"]
                        ),
                        None,
                    )
                    reasons = (
                        [
                            r
                            for r in matching.reasons
                            if r not in {"OUTBOUND_EXECUTOR_UNAVAILABLE", "past_occurrence_no_backlog"}
                        ]
                        if matching
                        else ["POLICY_RULE_MISSING"]
                    )
                    if reasons:
                        raise HTTPException(409, reasons[0])
                    destination = str(action.command["destination"])
                    if normalized_destination(destination) not in activation.destinations:
                        raise HTTPException(403, "RECIPIENT_NOT_ALLOWLISTED")
                    definition = PolicyDefinition.model_validate(version.definition)
                    rule = next(r for r in definition.rules if str(r.id) == action.command["rule_id"])
                    template = next(t for t in definition.templates if t.id == rule.template_id)
                    reserved = (
                        store.scalar(
                            select(func.count())
                            .select_from(EmailDispatchBinding)
                            .where(
                                EmailDispatchBinding.tenant_id == ctx.tenant_id,
                                EmailDispatchBinding.activation_id == activation.id,
                            )
                        )
                        or 0
                    )
                    if reserved >= activation.request_cap:
                        raise HTTPException(409, "SYNTHETIC_REQUEST_CAP")
                    operation = uuid5(
                        EMAIL_OPERATION_NAMESPACE, str(ctx.tenant_id) + ":" + action.operation_key
                    )
                    thread = store.scalar(
                        select(CommunicationThread).where(
                            CommunicationThread.tenant_id == ctx.tenant_id,
                            CommunicationThread.provider_connection_id == activation.email_connection_id,
                            CommunicationThread.origin_invoice_id == invoice.id,
                            CommunicationThread.purpose == "invoice_outbound",
                        )
                    )
                    if thread is None:
                        thread = CommunicationThread(
                            tenant_id=ctx.tenant_id,
                            provider_connection_id=activation.email_connection_id,
                            origin_invoice_id=invoice.id,
                            customer_id=invoice.customer_id,
                        )
                        store.add(thread)
                        store.flush()
                    mail = OutboundEmail(
                        tenant_id=ctx.tenant_id,
                        provider_connection_id=activation.email_connection_id,
                        origin_invoice_id=invoice.id,
                        conversation_thread_id=thread.id,
                        operation_id=operation,
                        sender=activation.sender,
                        to=(destination,),
                        subject=template.subject + " — " + invoice.number,
                        text_body=reminder_body(
                            template.body,
                            invoice.number,
                            invoice.balance_minor,
                            invoice.currency,
                            invoice.exponent,
                            invoice.due_date,
                            invoice.synthetic,
                        ),
                        message_id=f"<{operation}@aco.example.test>",
                        provider_thread_ref=ProviderReference(
                            provider="demo_email",
                            connection_id=activation.email_connection_id,
                            external_id=thread.native_thread_id,
                        )
                        if thread.native_thread_id and activation.provider_binding is None
                        else None,
                    )
                    if action.kind == "agent_email":
                        from aco.features.review.email_approvals import current_email_approval
                        from aco.features.review.email_dto import AgentEmailApprovalProposal

                        reviewed = current_email_approval(store, action)
                        exact = AgentEmailApprovalProposal.model_validate_json(json.dumps(reviewed.proposal))
                        mail = mail.model_copy(update={"subject": exact.subject, "text_body": exact.content})
                    boundary = "aco-" + operation.hex
                    original = compose_original(mail, boundary)
                    message = Communication(
                        tenant_id=ctx.tenant_id,
                        provider_connection_id=activation.email_connection_id,
                        thread_id=thread.id,
                        origin_invoice_id=invoice.id,
                        operation_id=operation,
                        content=mail.model_dump(mode="json", include=set(EmailContent.model_fields)),
                        event_at=now(),
                        synthetic=activation.capture_policy != "approved_email_test",
                    )
                    store.add(message)
                    store.flush()
                    store.add(
                        CommunicationInvoiceLink(
                            tenant_id=ctx.tenant_id,
                            communication_id=message.id,
                            invoice_id=invoice.id,
                            claim_or_context_key="origin",
                            provenance="origin_thread",
                            confidence="1",
                            evidence_excerpt=invoice.number,
                            evidence_offset=0,
                            classification="approved_agent_email"
                            if action.kind == "agent_email"
                            else "deterministic_reminder",
                        )
                    )
                    reservation = self.guard(store).reserve_contact(
                        ctx, invoice.id, destination, "email", operation
                    )
                    value = EmailDispatchBinding(
                        tenant_id=ctx.tenant_id,
                        action_id=action.id,
                        communication_id=message.id,
                        activation_id=activation.id,
                        email_connection_id=activation.email_connection_id,
                        storage_connection_id=activation.storage_connection_id,
                        operation_id=operation,
                        operation_key=action.operation_key,
                        payload_hash=payload_hash(mail),
                        source_revision=invoice.source_revision,
                        command=mail.model_dump(mode="json"),
                        mime_boundary=boundary,
                        mime_sha256=hashlib.sha256(original).hexdigest(),
                        contact_reservation_id=reservation.id,
                    )
                    store.add(value)
                    store.flush()
                    store.add(
                        EmailSubmission(tenant_id=ctx.tenant_id, binding_id=value.id, operation_id=operation)
                    )
                    IdentityService(store).audit(ctx, "email.prepared", message.id)
                    return value

                binding = await db.run_sync(build)
            assert binding is not None and action.lease_until is not None
            existing_source = await db.scalar(
                select(CommunicationSource).where(
                    CommunicationSource.tenant_id == ctx.tenant_id,
                    CommunicationSource.communication_id == binding.communication_id,
                    CommunicationSource.sha256 == binding.mime_sha256,
                )
            )
            artifact_id = (
                existing_source.artifact_id
                if existing_source
                else uuid5(binding.operation_id, "composed-original")
            )
            mail = OutboundEmail.model_validate_json(json.dumps(binding.command))
            original = compose_original(mail, binding.mime_boundary)
            actor = await db.get(Actor, ctx.actor_id)
            assert actor and ctx.job_id and ctx.lease_token
            grant = CommunicationCaptureGrant(
                tenant_id=ctx.tenant_id,
                actor_id=ctx.actor_id,
                actor_version=actor.version,
                tenant_generation=tenant.version,
                binding_id=binding.id,
                communication_id=binding.communication_id,
                storage_connection_id=binding.storage_connection_id,
                correlation_id=ctx.correlation_id,
                action_version=action.version,
                job_id=ctx.job_id,
                lease_token=ctx.lease_token,
                source_revision=binding.source_revision,
                objects=[
                    {
                        "artifact_id": str(artifact_id),
                        "source_kind": "provider_message",
                        "source_reference": str(binding.communication_id) + ":composed",
                        "sha256": binding.mime_sha256,
                        "content_type": "message/rfc822",
                        "size_bytes": len(original),
                        "operation": "put",
                    }
                ],
                capture_policy=activation.capture_policy,
                issued_at=now(),
                expires_at=min(action.lease_until, activation.expires_at),
            )
            db.add(grant)
            await db.flush()
            return binding.id, grant.id

    async def archive(self, ctx: ServiceContext, binding_id: UUID, grant_id: UUID) -> None:
        async with self.sessions.begin() as db:
            invocation = await db.run_sync(lambda store: capture_invocation(store, ctx, grant_id))
            binding = await db.get(EmailDispatchBinding, binding_id)
            grant = await db.get(CommunicationCaptureGrant, grant_id)
            assert binding and grant
            mail = OutboundEmail.model_validate_json(json.dumps(binding.command))
            original = compose_original(mail, binding.mime_boundary)
            material = grant.objects[0]
        value = await self.artifacts.store_ingested_artifact(
            invocation,
            grant_id,
            ArtifactWrite(
                artifact_id=UUID(str(material["artifact_id"])),
                source_kind="provider_message",
                source_reference=str(material["source_reference"]),
                content_type="message/rfc822",
                body=original,
                capture_class="real" if grant.capture_policy == "approved_email_test" else "synthetic_email",
            ),
            provider_outbound=self.email_runtime
            if isinstance(self.email_runtime, ProviderOutboundRuntime)
            else None,
        )
        async with self.sessions.begin() as db:
            await db.run_sync(lambda store: IdentityService(store).authorize(ctx, Permission.INVOICE_READ))
            prior = await db.scalar(
                select(CommunicationSource).where(
                    CommunicationSource.tenant_id == ctx.tenant_id,
                    CommunicationSource.communication_id == binding.communication_id,
                    CommunicationSource.sha256 == binding.mime_sha256,
                )
            )
            if prior is None:
                db.add(
                    CommunicationSource(
                        tenant_id=ctx.tenant_id,
                        communication_id=binding.communication_id,
                        artifact_id=value.id,
                        representation="ACO_COMPOSED",
                        sha256=binding.mime_sha256,
                        evidence={"producer": "python.email", "provider_original": False},
                    )
                )
            message = await db.get(Communication, binding.communication_id)
            assert message
            message.source_state = value.storage_state

    async def dispatch_email(
        self,
        db: AsyncSession,
        ctx: ServiceContext,
        command: ApprovedOutboundEmail,
        ticket: DispatchTicket,
        proposal: OutboundProposal,
    ) -> ActionResult:
        binding = await db.get(EmailDispatchBinding, command.binding_id)
        if (
            binding is None
            or binding.tenant_id != ctx.tenant_id
            or binding.action_id != ctx.action_id
            or command.payload_hash != binding.payload_hash
        ):
            raise HTTPException(403, "EMAIL_COMMAND_BINDING_MISMATCH")
        mail = OutboundEmail.model_validate_json(json.dumps(binding.command))
        await db.run_sync(lambda store: self.guard(store).consume_dispatch(ctx, ticket.id, proposal))
        submission = await db.scalar(
            select(EmailSubmission).where(
                EmailSubmission.tenant_id == ctx.tenant_id, EmailSubmission.binding_id == binding.id
            )
        )
        assert submission
        observation = None
        if isinstance(self.email_runtime, ProviderOutboundRuntime):
            port, observation = await self.email_runtime.port(db, ctx, binding.id, ticket, proposal)
        else:
            port = self.email_runtime.bind(ctx.tenant_id, binding.email_connection_id)
        capabilities = await port.describe_capabilities()
        if "send_email" not in capabilities.features or (
            observation is None and "durable_native_ledger" not in capabilities.features
        ):
            raise HTTPException(409, "EMAIL_CAPABILITY_REQUIRED")
        result = await ObservedEmail(db, self.sessions, port, observation=observation).send(
            ctx, mail, submission.id
        )
        await db.refresh(submission)
        if isinstance(self.email_runtime, ProviderOutboundRuntime):
            from aco.features.communications.submission_observation import observe_accepted_submission

            try:
                await observe_accepted_submission(db, self.email_runtime, ctx, binding.id)
            except HTTPException as error:
                # Read authorization failure cannot undo independently committed provider acceptance.
                # The denied read performs no wire; retain its safe reason without bypassing its guard.
                db.add(
                    CommunicationDeliveryEvent(
                        tenant_id=ctx.tenant_id,
                        communication_id=binding.communication_id,
                        provider_connection_id=binding.email_connection_id,
                        event_key=f"rfc-identity:{submission.id}",
                        state="rfc_identity_unavailable",
                        occurred_at=result.observed_at,
                        evidence={
                            "submission_id": str(submission.id),
                            "reason": str(error.detail),
                            "read_not_attempted": True,
                            "accepted_submission_unchanged": True,
                        },
                    )
                )
        message = await db.get(Communication, binding.communication_id)
        contact = await db.get(ContactReservation, binding.contact_reservation_id)
        assert message and contact
        message.native_message_id, message.delivery_state = result.message_ref.external_id, "accepted"
        contact.status, contact.submitted_at = "accepted", result.observed_at
        contact.evidence = {
            "submission_id": str(submission.id),
            "native_message_id": result.message_ref.external_id,
            "state": "provider_acceptance_not_delivery",
        }
        contact.version += 1
        action = await db.get(ScheduledAction, ctx.action_id)
        assert action
        if action.kind == "policy_due":
            occurrence = await db.get(PolicyOccurrence, action.occurrence_id)
            assert occurrence
            occurrence.state, occurrence.last_reason = "accepted", "provider_acceptance_not_delivery"
        db.add(
            CommunicationDeliveryEvent(
                tenant_id=ctx.tenant_id,
                communication_id=message.id,
                provider_connection_id=binding.email_connection_id,
                event_key=result.provider_ref.external_id,
                state="accepted",
                occurred_at=result.observed_at,
                evidence={
                    "native_identity": result.native_identity,
                    "reference_origin": result.reference_origin,
                    "message": result.message_ref.external_id,
                },
            )
        )
        return ActionResult(
            action_id=ctx.action_id,
            status=ActionStatus.SUCCEEDED,
            provider_ref=result.message_ref,
            changed_ids=(message.id,),
        )

    async def preserve_uncertainty(self, ctx: ServiceContext, action_id: UUID) -> None:
        """Reflect an existing durable submission fence after effect-transaction rollback."""
        async with self.sessions.begin() as db:
            action = await db.run_sync(lambda store: ActionService(store).locked(ctx, action_id))
            if (action.id, action.worker_actor_id, action.version, action.job_id, action.lease_token) != (
                ctx.action_id,
                ctx.actor_id,
                ctx.action_version,
                ctx.job_id,
                ctx.lease_token,
            ):
                return
            from aco.features.communications.uncertainty import project_email_uncertainty

            await db.run_sync(lambda store: project_email_uncertainty(store, action))

    async def _finish_failure(
        self,
        ctx: ServiceContext,
        lease: ActionLease,
        *,
        reason: str,
        failure: ProviderFailure | None = None,
    ) -> ActionResult:
        """Atomically finish the current attempt, releasing only proven terminal no-effect work."""
        from aco.features.identity.actors import require_job_binding
        from aco.features.policies.models import DispatchRead

        async with self.sessions.begin() as db:
            action = await db.run_sync(lambda store: ActionService(store).locked(ctx, lease.action_id))
            if (
                self.incarnation != PROCESS_INCARNATION
                or action.kind not in {"policy_due", "agent_email"}
                or action.status != "executing"
                or (action.id, action.worker_actor_id, action.version, action.job_id, action.lease_token)
                != (ctx.action_id, ctx.actor_id, ctx.action_version, ctx.job_id, ctx.lease_token)
                or (lease.action_id, lease.version, lease.token, lease.attempt)
                != (action.id, action.version, action.lease_token, action.attempts)
            ):
                raise HTTPException(409, "STALE_EXECUTION_LEASE")
            await db.run_sync(lambda store: require_job_binding(store, action))
            attempt = await db.scalar(
                select(ActionExecution).where(
                    ActionExecution.tenant_id == ctx.tenant_id,
                    ActionExecution.action_id == action.id,
                    ActionExecution.lease_token == lease.token,
                    ActionExecution.attempt == lease.attempt,
                    ActionExecution.status == "executing",
                )
            )
            if attempt is None:
                raise HTTPException(409, "STALE_EXECUTION_LEASE")
            foreign_incarnation = await db.scalar(
                select(DispatchRead.id)
                .where(
                    DispatchRead.tenant_id == ctx.tenant_id,
                    DispatchRead.attempt_id == attempt.id,
                    DispatchRead.incarnation != self.incarnation,
                )
                .limit(1)
            )
            if foreign_incarnation:
                raise HTTPException(409, "STALE_WORKER_INCARNATION")
            binding = await db.scalar(
                select(EmailDispatchBinding).where(
                    EmailDispatchBinding.tenant_id == ctx.tenant_id,
                    EmailDispatchBinding.action_id == action.id,
                )
            )
            submission = (
                await db.scalar(
                    select(EmailSubmission).where(
                        EmailSubmission.tenant_id == ctx.tenant_id,
                        EmailSubmission.binding_id == binding.id,
                    )
                )
                if binding
                else None
            )
            # No send can precede this action's committed binding/intent. A missing
            # submission for an existing binding is inconsistent, never no-effect proof.
            no_effect = binding is None or (
                submission is not None
                and submission.state == "prepared"
                and submission.intent_at is None
                and submission.usage_attempt_id is None
            )
            if submission is not None and submission.state == "no_effect" and submission.failure:
                persisted = ProviderFailure.model_validate_json(json.dumps(submission.failure))
                no_effect = not persisted.may_have_side_effect
            if not no_effect:
                failure = ProviderFailure(
                    code=ProviderErrorCode.UNKNOWN_SIDE_EFFECT, retryable=False, may_have_side_effect=True
                )
            if failure is not None:
                await db.run_sync(lambda store: ActionService(store).fail_action(ctx, lease, failure))
            else:
                await db.run_sync(
                    lambda store: ActionService(store).complete_action(
                        ctx,
                        lease,
                        ActionResult(action_id=action.id, status=ActionStatus.FAILED, error_code=reason),
                    )
                )
            if no_effect and action.status in {"failed", "dead_letter"}:
                evidence = {
                    "verified_no_submission": submission is None or submission.intent_at is None,
                    "verified_no_effect": True,
                    "reason": reason,
                    "submission_id": str(submission.id) if submission else None,
                }
                if binding:
                    contact = await db.get(ContactReservation, binding.contact_reservation_id)
                    message = await db.get(Communication, binding.communication_id)
                    assert contact and message
                    if contact.status == "reserved":
                        contact.status, contact.released_at = "released", now()
                        contact.evidence = evidence
                        contact.version += 1
                    message.delivery_state = "failed"
                    db.add(
                        CommunicationDeliveryEvent(
                            tenant_id=ctx.tenant_id,
                            communication_id=message.id,
                            provider_connection_id=binding.email_connection_id,
                            event_key="terminal-no-effect:" + str(attempt.id),
                            state="failed",
                            occurred_at=now(),
                            evidence=evidence,
                        )
                    )
                occurrence = await db.get(PolicyOccurrence, action.occurrence_id)
                if occurrence:
                    occurrence.state, occurrence.last_reason, occurrence.reconsider_after = (
                        "pending_recompute",
                        reason,
                        now() + timedelta(days=1),
                    )
            elif not no_effect and binding and submission:
                message = await db.get(Communication, binding.communication_id)
                contact = await db.get(ContactReservation, binding.contact_reservation_id)
                assert message and contact
                message.delivery_state = "unknown_outcome"
                contact.status, contact.submitted_at = "unknown_outcome", submission.intent_at
                contact.evidence = {
                    "submission_id": str(submission.id),
                    "reconciliation_required": True,
                    "independent_state": submission.state,
                }
            assert action.result
            return ActionResult.model_validate_json(json.dumps(action.result))

    async def process_reminder_due(
        self, ctx: ServiceContext, action_id: UUID, lease: ActionLease
    ) -> ActionResult:
        try:
            binding_id, grant_id = await self.prepare(ctx, action_id)
            if grant_id.int == 0:
                raise HTTPException(409, "SOURCE_VERSION_CHANGED")
            await self.archive(ctx, binding_id, grant_id)
            async with self.sessions.begin() as db:
                await db.run_sync(
                    lambda store: IdentityService(store).authorize(ctx, Permission.INVOICE_READ)
                )
                action = await db.get(ScheduledAction, action_id)
                binding = await db.get(EmailDispatchBinding, binding_id)
                attempt = await db.scalar(
                    select(ActionExecution).where(
                        ActionExecution.action_id == action_id, ActionExecution.lease_token == ctx.lease_token
                    )
                )
                assert action and binding and attempt
                read_id = await read_dispatch_source(
                    db,
                    ctx,
                    action.invoice_id,
                    attempt.id,
                    self.incarnation,
                    DemoAccountingAdapter(db, action.provider_connection_id),
                )
                invoice = await db.get(InvoiceSnapshot, action.invoice_id, populate_existing=True)
                assert invoice
                if invoice.source_revision != action.source_revision:
                    result = ActionResult(
                        action_id=action.id, status=ActionStatus.FAILED, error_code="SOURCE_VERSION_CHANGED"
                    )
                else:
                    approval_id = None
                    if action.kind == "agent_email":
                        from aco.features.review.email_approvals import current_email_approval

                        reviewed = await db.run_sync(lambda store: current_email_approval(store, action))
                        approval_id = reviewed.id
                    proposal = OutboundProposal(
                        approval_id=approval_id,
                        action_id=action.id,
                        attempt_id=attempt.id,
                        read_id=read_id,
                        destination=str(action.command["destination"]),
                    )
                    ticket = await db.run_sync(
                        lambda store: self.guard(store).reserve_dispatch(
                            ctx, self.guard(store).evaluate_outbound(ctx, proposal, now()), proposal
                        )
                    )
                    result = await self.dispatch_email(
                        db,
                        ctx,
                        ApprovedOutboundEmail(binding_id=binding.id, payload_hash=binding.payload_hash),
                        ticket,
                        proposal,
                    )
                await db.run_sync(lambda store: ActionService(store).complete_action(ctx, lease, result))
                return result
        except (EmailFailure, ActionFailure) as error:
            return await self._finish_failure(
                ctx, lease, reason=error.failure.code.value, failure=error.failure
            )
        except (HTTPException, StorageFailure) as error:
            reason = str(error.detail) if isinstance(error, HTTPException) else error.failure.code.value
            # Archive's storage-side effect is not an email submission. The finalizer
            # independently inspects email intent before permitting a bounded retry.
            failure = (
                ProviderFailure(
                    code=error.failure.code,
                    retryable=error.failure.retryable,
                    may_have_side_effect=False,
                    retry_after=error.failure.retry_after,
                    request_id=error.failure.request_id,
                )
                if isinstance(error, StorageFailure)
                else None
            )
            return await self._finish_failure(ctx, lease, reason=reason, failure=failure)

        except Exception:
            await self.preserve_uncertainty(ctx, action_id)
            raise
