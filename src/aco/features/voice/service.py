"""Current-human voice preparation and exact source/session authority; no role promotion."""

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.core.types import Role
from aco.features.accounting.models import InvoiceSnapshot
from aco.features.actions.models import ScheduledAction
from aco.features.agent.context import current_run
from aco.features.artifacts.consent import (
    ConsentService,
    consent_command,
    consent_reasons,
    voice_read_allowed,
)
from aco.features.artifacts.consent_models import ConsentEvidence, VoiceArtifactLink, VoicePostCall
from aco.features.artifacts.dto import ArtifactRef
from aco.features.artifacts.models import Artifact
from aco.features.identity.models import Actor, AuditEvent, Membership, ProviderConnection, Tenant, now
from aco.features.identity.service import ROLE_PERMISSIONS, IdentityService, Permission
from aco.features.policies.contact_stop import pending_contact_stops
from aco.features.review.approvals import HumanApprovalService, digest, stamp
from aco.features.review.models import ApprovalRequest
from aco.features.tools.authorization import AgentActionContext, require_agent_action
from aco.features.voice.dto import (
    PrepareVoice,
    VoiceApprovalProposal,
    VoiceConnectionView,
    VoiceEventView,
    VoiceInvocationView,
    VoiceOutcomeView,
    VoicePage,
    VoiceSessionView,
    VoiceSourceView,
)
from aco.features.voice.models import (
    VoiceBusinessEvidence,
    VoiceEventRecord,
    VoiceInvocation,
    VoiceInvoiceLink,
    VoiceSession,
)

if TYPE_CHECKING:
    from aco.features.voice.agent_request import RequestVoiceCall
    from aco.features.voice.runtime import VoiceRuntime


def source_links(db: Session, row: VoiceSession) -> list[VoiceInvoiceLink]:
    return list(
        db.scalars(
            select(VoiceInvoiceLink).where(
                VoiceInvoiceLink.tenant_id == row.tenant_id, VoiceInvoiceLink.session_id == row.id
            )
        )
    )


def current_material(
    db: Session, row: VoiceSession, *, allow_own_stop_completion: bool = False, at: datetime | None = None
) -> tuple[dict[str, object], list[str]]:
    at = at or now()
    tenant = db.get(Tenant, row.tenant_id, populate_existing=True)
    member = db.scalar(
        select(Membership)
        .where(Membership.tenant_id == row.tenant_id, Membership.user_id == row.initiator_id)
        .execution_options(populate_existing=True)
    )
    actor = db.get(Actor, row.actor_id, populate_existing=True)
    human = db.get(Actor, row.initiator_id, populate_existing=True)
    connection = db.get(ProviderConnection, row.connection_id, populate_existing=True)
    reasons = []
    if (
        tenant is None
        or not tenant.is_demo
        or tenant.version != row.tenant_version
        or member is None
        or not member.active
        or member.version != row.membership_version
        or Permission.INVESTIGATE not in ROLE_PERMISSIONS[Role(member.role)]
        or human is None
        or not human.active
        or actor is None
        or not actor.active
        or actor.kind != "service"
        or actor.purpose != "voice_callback"
        or actor.tenant_id != row.tenant_id
        or actor.version != row.actor_version
    ):
        reasons.append("VOICE_AUTHORITY_REVOKED")
    if (
        connection is None
        or connection.tenant_id != row.tenant_id
        or connection.provider != "vapi"
        or connection.provider_type != "voice"
        or connection.status != "READY"
        or connection.version != row.connection_version
        or connection.encrypted_credentials_ref != row.credential_revision
    ):
        reasons.append("VOICE_CONNECTION_CHANGED")
    links = source_links(db, row)
    command = PrepareVoice.model_validate_json(json.dumps(row.command))
    if {link.invoice_id for link in links} != set(command.invoice_ids):
        reasons.append("VOICE_SCOPE_CHANGED")
    invoices = []
    contact_stops = []
    customers = set()
    for link in links:
        invoice = db.get(InvoiceSnapshot, link.invoice_id, populate_existing=True)
        account = db.get(ProviderConnection, link.accounting_connection_id, populate_existing=True)
        if (
            invoice is None
            or invoice.tenant_id != row.tenant_id
            or not invoice.synthetic
            or not invoice.verified
            or invoice.source_revision != link.source_revision
            or invoice.provider_connection_id != link.accounting_connection_id
            or invoice.last_read_at is None
            or now() - invoice.last_read_at > timedelta(minutes=5)
            or invoice.last_read_at > now() + timedelta(seconds=5)
            or account is None
            or account.status != "READY"
            or account.tenant_id != row.tenant_id
            or account.version != link.connection_version
        ):
            reasons.append("VOICE_SOURCE_CHANGED_OR_STALE")
        if invoice:
            stops = pending_contact_stops(db, invoice)
            if allow_own_stop_completion:
                remaining_stops = []
                for stop in stops:
                    stopped_by = db.get(ScheduledAction, UUID(str(stop["action_id"])))
                    if (
                        stopped_by is None
                        or stopped_by.kind != "voice_internal"
                        or stopped_by.voice_session_id != row.id
                    ):
                        remaining_stops.append(stop)
                stops = remaining_stops
            if stops:
                reasons.append("VOICE_CONTACT_STOP_PENDING_REVIEW")
                contact_stops.append({"invoice_id": str(invoice.id), "restrictions": stops})
            customers.add(invoice.customer_id)
            invoices.append(
                stamp(
                    invoice,
                    "customer_id",
                    "source_revision",
                    "balance_minor",
                    "currency",
                    "exponent",
                    "verified",
                )
            )
    if len(customers) != 1:
        reasons.append("VOICE_CUSTOMER_SCOPE_CHANGED")
    binding: dict[str, object] = {
        "session_id": str(row.id),
        "command": row.command,
        "binding_hash": row.binding_hash,
        "tenant": stamp(tenant, "is_demo"),
        "member": stamp(member, "user_id", "role", "active"),
        "actor": stamp(actor, "active", "purpose"),
        "human": stamp(human, "active"),
        "connection": stamp(connection, "status", "encrypted_credentials_ref"),
        "invoices": sorted(invoices, key=lambda item: str(item)),
        "customer_contact_stops": contact_stops,
        "expires_at": row.expires_at.isoformat(),
    }
    if command.consent_id is not None:
        consent = db.get(ConsentEvidence, command.consent_id, populate_existing=True)
        if (
            consent is None
            or consent.tenant_id != row.tenant_id
            or consent.voice_connection_id != row.connection_id
            or customers != {consent.customer_id}
        ):
            reasons.append("VOICE_CONSENT_SCOPE_DENIED")
        else:
            reasons.extend(consent_reasons(db, consent))
            binding["consent"] = {
                "id": str(consent.id),
                "material_hash": consent.material_hash,
                "command": consent.command,
            }
    if row.live_grant_id is not None:
        from aco.features.voice.live import current_grant
        from aco.features.voice.live_policy import voice_policy_snapshot

        try:
            grant = current_grant(db, row.live_grant_id, row.tenant_id, row.connection_id, require_ready=True)
            live_material, live_reasons = voice_policy_snapshot(
                db,
                grant,
                command.invoice_ids,
                at,
                exclude_operation_id=row.request_id,
                ongoing_session_id=row.id if row.state in {"active", "connected"} else None,
            )
            binding["live"] = live_material
            # Ongoing calls may complete their own typed business facts. New outreach
            # eligibility is enforced again immediately before create/join; source,
            # recipient, current policy, authority and contact stops remain pinned here.
            reasons.extend(live_reasons)
        except HTTPException as exc:
            reasons.append(str(exc.detail))
    return binding, reasons


def voice_scope(
    db: Session,
    approval: ApprovalRequest,
    *,
    allow_own_stop_completion: bool = False,
    at: datetime | None = None,
) -> tuple[VoiceApprovalProposal, ScheduledAction, InvoiceSnapshot, dict[str, object], list[str]]:
    proposal = VoiceApprovalProposal.model_validate_json(json.dumps(approval.proposal))
    action = db.get(ScheduledAction, approval.action_id, populate_existing=True)
    row = db.get(VoiceSession, proposal.session_id, populate_existing=True)
    invoice = db.get(InvoiceSnapshot, proposal.invoice_ids[0], populate_existing=True)
    if (
        row is None
        or action is None
        or invoice is None
        or row.tenant_id != approval.tenant_id
        or action.tenant_id != row.tenant_id
        or invoice.tenant_id != row.tenant_id
    ):
        raise HTTPException(403, "VOICE_APPROVAL_SCOPE_DENIED")
    binding, reasons = current_material(db, row, allow_own_stop_completion=allow_own_stop_completion, at=at)
    if (
        action.kind != "voice_session"
        or action.voice_session_id != row.id
        or action.voice_invocation_id is not None
        or approval.id != row.approval_id
        or action.id != proposal.action_id
        or action.version != proposal.action_version
        or approval.action_version != action.version
        or approval.material_hash != digest(approval.proposal)
        or action.command != row.command
        or approval.initiator_id != row.initiator_id
        or approval.proposer_id != row.actor_id
        or action.worker_actor_id != row.actor_id
        or action.initiator_id != row.initiator_id
        or action.invoice_id != proposal.invoice_ids[0]
        or proposal.model_dump(mode="json")["invoice_ids"] != row.command["invoice_ids"]
        or proposal.objective != row.command["objective"]
        or proposal.demo_scenario != row.command.get("demo_scenario", "task")
        or (str(proposal.consent_id) if proposal.consent_id else None) != row.command.get("consent_id")
    ):
        reasons.append("VOICE_APPROVAL_MATERIAL_CHANGED")
    if proposal.consent_id is not None:
        consent = db.get(ConsentEvidence, proposal.consent_id, populate_existing=True)
        if (
            consent is None
            or proposal.consent_hash != consent.material_hash
            or proposal.capture_scopes != consent_command(consent).scopes
            or proposal.capture != "SYNTHETIC_ARTIFACTS"
        ):
            reasons.append("VOICE_APPROVAL_CONSENT_CHANGED")
    elif (
        proposal.capture != "DISABLED"
        or proposal.capture_scopes is not None
        or proposal.consent_hash is not None
    ):
        reasons.append("VOICE_APPROVAL_CONSENT_CHANGED")
    if (at or now()) >= row.expires_at:
        reasons.append("VOICE_SESSION_EXPIRED")
    if row.state in {"failed", "unknown_outcome"}:
        reasons.append("VOICE_SESSION_UNAVAILABLE")
    return proposal, action, invoice, binding, reasons


class VoiceService:
    def __init__(self, db: Session, runtime: "VoiceRuntime"):
        self.db, self.runtime = db, runtime

    def human(self, ctx: TenantContext, permission: Permission) -> Tenant:
        if not isinstance(ctx, TenantContext):
            raise HTTPException(403, "HUMAN_REQUIRED")
        return IdentityService(self.db).authorize(ctx, permission)

    def prepare_call(self, ctx: TenantContext, command: PrepareVoice) -> VoiceSessionView:
        tenant = self.human(ctx, Permission.INVESTIGATE)
        return self._prepare_call(ctx, command, tenant, ctx.actor_id)

    def prepare_agent_call(self, ctx: AgentActionContext, request: "RequestVoiceCall") -> VoiceSessionView:
        action = require_agent_action(self.db, ctx, "request_voice_call", request)
        from aco.features.voice.agent_request import validate_voice_request

        validate_voice_request(self.db, ctx.authority, request)
        run = current_run(self.db, ctx.authority)
        candidates = self.db.scalars(
            select(ProviderConnection).where(
                ProviderConnection.tenant_id == ctx.tenant_id,
                ProviderConnection.provider == "vapi",
                ProviderConnection.provider_type == "voice",
                ProviderConnection.status == "READY",
            )
        )
        usable = []
        for candidate in candidates:
            try:
                binding = self.runtime.binding(ctx.tenant_id, candidate.id)
            except HTTPException:
                continue
            if (
                candidate.encrypted_credentials_ref == binding.credential_revision
                and "probe:" + self.runtime.binding_hash(binding) in candidate.capabilities
            ):
                usable.append(candidate)
        if len(usable) != 1:
            raise HTTPException(403, "VOICE_REVIEW_CONNECTION_UNAVAILABLE")
        command = PrepareVoice(
            request_id=uuid5(action.id, "voice-review-request-v1"),
            connection_id=usable[0].id,
            invoice_ids=request.invoice_ids,
            objective=request.objective,
            consent_id=None,
        )
        tenant = self.db.get(Tenant, run.tenant_id, populate_existing=True)
        assert tenant is not None
        return self._prepare_call(ctx, command, tenant, run.initiator_id)

    def _prepare_call(
        self,
        ctx: TenantContext | AgentActionContext,
        command: PrepareVoice,
        tenant: Tenant,
        initiator_id: UUID,
    ) -> VoiceSessionView:
        if not tenant.is_demo:
            raise HTTPException(403, "VOICE_SYNTHETIC_ONLY")
        binding = self.runtime.binding(ctx.tenant_id, command.connection_id)
        old = self.db.scalar(
            select(VoiceSession).where(
                VoiceSession.tenant_id == ctx.tenant_id, VoiceSession.request_id == command.request_id
            )
        )
        if old:
            if old.command != command.model_dump(mode="json", exclude_none=True):
                raise HTTPException(409, "VOICE_REPLAY_CONFLICT")
            return self.view(old)
        if len(set(command.invoice_ids)) != len(command.invoice_ids):
            raise HTTPException(422, "VOICE_DUPLICATE_SCOPE")
        connection = self.db.get(ProviderConnection, command.connection_id, populate_existing=True)
        if (
            connection is None
            or connection.tenant_id != ctx.tenant_id
            or connection.provider != "vapi"
            or connection.provider_type != "voice"
            or connection.status != "READY"
            or connection.encrypted_credentials_ref != binding.credential_revision
        ):
            raise HTTPException(403, "VOICE_CONNECTION_UNAVAILABLE")
        invoices = list(
            self.db.scalars(
                select(InvoiceSnapshot).where(
                    InvoiceSnapshot.tenant_id == ctx.tenant_id, InvoiceSnapshot.id.in_(command.invoice_ids)
                )
            )
        )
        if len(invoices) != len(command.invoice_ids) or len({i.customer_id for i in invoices}) != 1:
            raise HTTPException(403, "VOICE_INVOICE_SCOPE_DENIED")
        if command.demo_scenario == "business_facts":
            primary = next(i for i in invoices if i.id == command.invoice_ids[0])
            if (primary.currency, primary.exponent) != ("AUD", 2) or primary.balance_minor < 4000:
                raise HTTPException(422, "VOICE_FIXED_FACTS_REQUIRES_AUD40_OUTSTANDING")
        member = self.db.scalar(
            select(Membership).where(
                Membership.tenant_id == ctx.tenant_id, Membership.user_id == initiator_id
            )
        )
        assert member is not None
        actor = self.db.scalar(
            select(Actor).where(Actor.tenant_id == ctx.tenant_id, Actor.purpose == "voice_callback")
        )
        if actor is None:
            actor = Actor(kind="service", tenant_id=ctx.tenant_id, purpose="voice_callback", active=True)
            self.db.add(actor)
            self.db.flush()
        live_grant_id, expires_at = self.runtime.preparation_binding(self.db, command, initiator_id)
        row = VoiceSession(
            live_grant_id=live_grant_id,
            tenant_id=ctx.tenant_id,
            request_id=command.request_id,
            connection_id=connection.id,
            connection_version=connection.version,
            credential_revision=binding.credential_revision,
            binding_hash=self.runtime.binding_hash(binding),
            initiator_id=initiator_id,
            membership_version=member.version,
            tenant_version=tenant.version,
            actor_id=actor.id,
            actor_version=actor.version,
            correlation_id=ctx.correlation_id,
            command=command.model_dump(mode="json", exclude_none=True),
            expires_at=expires_at,
            state="awaiting_approval",
        )
        self.db.add(row)
        self.db.flush()
        for invoice in invoices:
            account = self.db.get(ProviderConnection, invoice.provider_connection_id)
            assert account
            self.db.add(
                VoiceInvoiceLink(
                    tenant_id=ctx.tenant_id,
                    session_id=row.id,
                    invoice_id=invoice.id,
                    source_revision=invoice.source_revision,
                    accounting_connection_id=account.id,
                    connection_version=account.version,
                )
            )
        self.db.flush()
        _, reasons = current_material(self.db, row, at=self.runtime.clock.now())
        if reasons:
            raise HTTPException(403, reasons[0])
        primary = next(i for i in invoices if i.id == command.invoice_ids[0])
        action = self.action(row, primary, "voice_session", None)
        action.status = "awaiting_approval"
        self.db.add(action)
        self.db.flush()
        consent = self.db.get(ConsentEvidence, command.consent_id) if command.consent_id else None
        proposal = VoiceApprovalProposal(
            action_id=action.id,
            action_version=action.version,
            session_id=row.id,
            invoice_ids=command.invoice_ids,
            objective=command.objective,
            demo_scenario=command.demo_scenario,
            consent_id=consent.id if consent else None,
            consent_hash=consent.material_hash if consent else None,
            capture_scopes=consent_command(consent).scopes if consent else None,
            capture="SYNTHETIC_ARTIFACTS" if consent else "DISABLED",
        )
        proposal = VoiceApprovalProposal.model_validate(
            {**proposal.model_dump(), **self.runtime.approval_metadata(self.db, row)}
        )
        approval = ApprovalRequest(
            tenant_id=ctx.tenant_id,
            action_id=action.id,
            action_version=action.version,
            proposer_id=actor.id,
            initiator_id=initiator_id,
            material_hash=digest(proposal.model_dump(mode="json")),
            proposal=proposal.model_dump(mode="json"),
            status="pending",
        )
        self.db.add(approval)
        self.db.flush()
        row.approval_id = approval.id
        self.db.add(
            AuditEvent(
                tenant_id=ctx.tenant_id,
                actor_id=ctx.actor_id,
                correlation_id=ctx.correlation_id,
                action="voice.prepared",
                target_id=row.id,
                details={"evidence_mode": binding.evidence_mode, "capture": proposal.capture},
            )
        )
        self.db.flush()
        return self.view(row)

    def action(
        self, row: VoiceSession, invoice: InvoiceSnapshot, kind: str, invocation: VoiceInvocation | None
    ) -> ScheduledAction:
        account = self.db.get(ProviderConnection, invoice.provider_connection_id)
        assert account
        tenant = self.db.get(Tenant, row.tenant_id)
        assert tenant
        return ScheduledAction(
            tenant_id=row.tenant_id,
            provider_connection_id=account.id,
            invoice_id=invoice.id,
            source_revision=invoice.source_revision,
            policy_generation=0,
            initiator_id=row.initiator_id,
            membership_version=row.membership_version,
            tenant_version=row.tenant_version,
            outbound_paused=tenant.outbound_paused,
            automation_enabled=tenant.automation_enabled,
            worker_actor_id=row.actor_id,
            actor_version=row.actor_version,
            connection_version=account.version,
            correlation_id=row.correlation_id,
            kind=kind,
            operation_key=f"voice:{row.id}:{invocation.id if invocation else 'start'}",
            command=invocation.arguments if invocation else row.command,
            due_at=now(),
            status="executing",
            attempts=0,
            voice_session_id=row.id,
            voice_invocation_id=invocation.id if invocation else None,
        )

    def get(self, ctx: TenantContext, key: UUID) -> VoiceSessionView:
        self.human(ctx, Permission.INVOICE_READ)
        row = self.db.get(VoiceSession, key)
        if row is None or row.tenant_id != ctx.tenant_id:
            raise HTTPException(404, "VOICE_SESSION_NOT_FOUND")
        view = self.view(row)
        reasons: list[str] = []
        try:
            self.human(ctx, Permission.INVESTIGATE)
            binding = self.runtime.binding(row.tenant_id, row.connection_id)
            self.runtime.start_available(self.db, ctx, row)
            if self.runtime.binding_hash(binding) != row.binding_hash:
                reasons.append("VOICE_CONFIGURATION_CHANGED")
            approval = self.db.get(ApprovalRequest, row.approval_id)
            if approval is None or not HumanApprovalService(self.db)._view(ctx, approval).approval_valid:
                reasons.append("VOICE_APPROVAL_REQUIRED")
        except HTTPException as exc:
            reasons.append(str(exc.detail))
        if row.state != "awaiting_approval":
            reasons.append("VOICE_ALREADY_STARTED_OR_TERMINAL")
        reconciliation_reasons: list[str] = []
        from aco.features.voice.postcall import completed, permitted
        from aco.integrations.ports.voice_artifacts import VoiceArtifactRequest

        try:
            self.human(ctx, Permission.INVESTIGATE)
            completed(self.db, ctx, key)
            event = self.db.scalar(
                select(VoiceEventRecord)
                .where(VoiceEventRecord.session_id == row.id, VoiceEventRecord.kind == "call.completed")
                .order_by(VoiceEventRecord.created_at)
                .limit(1)
            )
            if event is None:
                raise HTTPException(409, "VOICE_POSTCALL_SOURCE_EVENT_REQUIRED")
            if view.consent is not None:
                for kind in ("transcript", "recording"):
                    scopes = view.consent.command.scopes
                    if not (scopes.transcript if kind == "transcript" else scopes.audio_capture):
                        continue
                    assert row.provider_call_id
                    permitted(
                        self.db,
                        ctx,
                        VoiceArtifactRequest(
                            tenant_id=row.tenant_id,
                            connection_id=row.connection_id,
                            session_id=row.id,
                            consent_id=view.consent.id,
                            provider_call_id=row.provider_call_id,
                            captured_at=event.created_at,
                            kind=kind,
                        ),
                        self.runtime,
                    )
        except HTTPException as exc:
            reconciliation_reasons.append(str(exc.detail))
        return view.model_copy(
            update={
                "can_start": not reasons,
                "reasons": reasons,
                "can_reconcile": not reconciliation_reasons,
                "reconciliation_reasons": reconciliation_reasons,
                **self.runtime.control_permissions(self.db, ctx, row),
            }
        )

    def list(self, ctx: TenantContext, invoice_id: UUID | None = None) -> VoicePage:
        self.human(ctx, Permission.INVOICE_READ)
        rows = self.db.scalars(
            select(VoiceSession)
            .where(VoiceSession.tenant_id == ctx.tenant_id)
            .order_by(VoiceSession.created_at.desc())
            .limit(50)
        )
        items = [
            self.get(ctx, row.id)
            for row in rows
            if invoice_id is None
            or invoice_id in PrepareVoice.model_validate_json(json.dumps(row.command)).invoice_ids
        ]
        choices = []
        reasons = []
        try:
            self.human(ctx, Permission.INVESTIGATE)
        except HTTPException:
            reasons.append("INVESTIGATE_PERMISSION_REQUIRED")
        if not reasons:
            for connection in self.db.scalars(
                select(ProviderConnection).where(
                    ProviderConnection.tenant_id == ctx.tenant_id,
                    ProviderConnection.provider_type == "voice",
                    ProviderConnection.provider == "vapi",
                    ProviderConnection.status == "READY",
                )
            ):
                try:
                    binding = self.runtime.connection_available(self.db, ctx, connection.id)
                    choices.append(
                        VoiceConnectionView(
                            id=connection.id,
                            label="Vapi — LIVE browser, capture off"
                            if connection.external_account_id and binding.evidence_mode == "LIVE"
                            else "Vapi — mocked HTTP, capture off",
                        )
                    )
                except HTTPException:
                    pass
        if not choices:
            reasons.append(
                "VOICE_LIVE_CONNECTION_CURRENTLY_UNAVAILABLE"
                if self.runtime.evidence_mode == "LIVE"
                else "VOICE_OFFLINE_RUNTIME_UNAVAILABLE"
            )
        setup_reasons = []
        try:
            self.runtime.setup_authorized(self.db, ctx)
        except HTTPException as exc:
            setup_reasons.append(str(exc.detail))
        return VoicePage(
            evidence_mode=self.runtime.evidence_mode,
            items=items,
            connections=choices,
            can_prepare=not reasons,
            reasons=reasons,
            can_setup=not setup_reasons,
            setup_reasons=setup_reasons,
        )

    def view(self, row: VoiceSession) -> VoiceSessionView:
        from aco.features.voice.evidence import source_view

        command = PrepareVoice.model_validate_json(json.dumps(row.command))
        assert row.approval_id
        ids = []
        for invocation in self.db.scalars(
            select(VoiceInvocation).where(VoiceInvocation.session_id == row.id)
        ):
            if invocation.result and invocation.result.get("task_id"):
                ids.append(UUID(str(invocation.result["task_id"])))
        post = self.db.scalar(
            select(VoicePostCall).where(
                VoicePostCall.tenant_id == row.tenant_id, VoicePostCall.session_id == row.id
            )
        )
        originals = list(
            self.db.scalars(
                select(Artifact)
                .join(VoiceArtifactLink, VoiceArtifactLink.artifact_id == Artifact.id)
                .where(VoiceArtifactLink.tenant_id == row.tenant_id, VoiceArtifactLink.session_id == row.id)
            )
        )
        available = [
            a
            for a in originals
            if a.storage_state == "stored"
            and a.revoked_at is None
            and a.expires_at > now()
            and voice_read_allowed(self.db, a)
        ]
        outcomes: list[VoiceOutcomeView] = []
        for invocation in self.db.scalars(
            select(VoiceInvocation).where(
                VoiceInvocation.tenant_id == row.tenant_id, VoiceInvocation.session_id == row.id
            )
        ):
            result = invocation.result
            action = self.db.get(ScheduledAction, invocation.action_id) if invocation.action_id else None
            if (
                not result
                or not action
                or action.tenant_id != row.tenant_id
                or action.voice_session_id != row.id
                or action.voice_invocation_id != invocation.id
                or action.status != "succeeded"
                or action.result != result
            ):
                continue
            kind = result.get("record_kind")
            ids_value = result.get("changed_ids")
            if invocation.tool == "create_collection_task" and isinstance(result.get("task_id"), str):
                kind, ids_value = "task", [result["task_id"]]
            if kind not in {"task", "case", "promise", "hold"} or not isinstance(ids_value, list):
                continue
            outcomes.append(
                VoiceOutcomeView.model_validate_json(
                    json.dumps(
                        dict(
                            invocation_id=str(invocation.id),
                            action_id=str(action.id),
                            invoice_id=str(action.invoice_id),
                            record_kind=kind,
                            changed_ids=ids_value,
                        )
                    )
                )
            )
        return VoiceSessionView(
            stop_outcome=self.runtime.stop_outcome(self.db, row),
            id=row.id,
            version=row.version,
            approval_id=row.approval_id,
            connection_id=row.connection_id,
            invoice_ids=command.invoice_ids,
            objective=command.objective,
            demo_scenario=command.demo_scenario,
            consent=ConsentService(self.db, self.runtime).view(consent)
            if command.consent_id and (consent := self.db.get(ConsentEvidence, command.consent_id))
            else None,
            communication_id=post.communication_id if post else None,
            artifacts=[
                ArtifactRef(**{key: getattr(a, key) for key in ArtifactRef.model_fields}) for a in originals
            ],
            capture_evidence="FAKE" if originals else None,
            transcript_available=any(a.content_type == "application/json" for a in available),
            recording_available=any(a.content_type.startswith("audio/") for a in available),
            evidence_mode="LIVE" if row.live_grant_id is not None else "MOCKED_HTTP",
            live_status="SELECTED" if row.live_grant_id is not None else "BLOCKED",
            state=row.state,
            provider_call_id=row.provider_call_id,
            expires_at=row.expires_at,
            changed_ids=ids,
            outcomes=outcomes,
            business_evidence=[
                source
                for key in self.db.scalars(
                    select(VoiceBusinessEvidence.id)
                    .where(
                        VoiceBusinessEvidence.tenant_id == row.tenant_id,
                        VoiceBusinessEvidence.session_id == row.id,
                    )
                    .order_by(VoiceBusinessEvidence.created_at, VoiceBusinessEvidence.id)
                )
                if (source := source_view(self.db, key)) is not None
            ],
            invocations=[
                VoiceInvocationView(
                    id=i.id,
                    native_id=i.native_id,
                    tool=i.tool,
                    arguments=i.arguments,
                    result=i.result,
                    action_id=i.action_id,
                    created_at=i.created_at,
                )
                for i in self.db.scalars(
                    select(VoiceInvocation)
                    .where(VoiceInvocation.session_id == row.id)
                    .order_by(VoiceInvocation.created_at, VoiceInvocation.id)
                )
            ],
            events=[
                VoiceEventView(id=e.id, kind=e.kind, created_at=e.created_at)
                for e in self.db.scalars(
                    select(VoiceEventRecord)
                    .where(VoiceEventRecord.session_id == row.id)
                    .order_by(VoiceEventRecord.created_at, VoiceEventRecord.id)
                )
            ],
            source_scope=[
                VoiceSourceView(
                    invoice_id=link.invoice_id,
                    source_revision=link.source_revision,
                    captured_at=link.created_at,
                )
                for link in source_links(self.db, row)
            ],
        )
