"""Independent human decisions. Approval validity is necessary, never dispatch authority."""

import builtins
import hashlib
import json
from datetime import datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import false, select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.core.types import Role
from aco.features.accounting.models import AccountingEvent, CollectionRecord, CustomerContact, InvoiceSnapshot
from aco.features.actions.models import ScheduledAction
from aco.features.identity.models import Actor, Membership, ProviderConnection, Tenant, now
from aco.features.identity.service import ROLE_PERMISSIONS, IdentityService, Permission
from aco.features.policies.contact_stop import pending_contact_stops
from aco.features.policies.models import (
    CollectionHold,
    CollectionPolicy,
    CollectionPolicyVersion,
    RecipientControl,
)
from aco.features.promises.models import CustomerScopeReview
from aco.features.receivables.queries import MoneyJSON
from aco.features.review.approval_dto import (
    AgentApprovalEvidence,
    ApprovalDecisionCommand,
    ApprovalDecisionView,
    ApprovalEvidenceView,
    ApprovalInvoiceScope,
    ApprovalPage,
    ApprovalView,
    InternalTaskApprovalProposal,
)
from aco.features.review.email_dto import AgentEmailApprovalProposal
from aco.features.review.internal_work import ApprovalProposal
from aco.features.review.models import ApprovalDecision, ApprovalRequest
from aco.features.voice.dto import VoiceApprovalProposal


def digest(value: dict[str, object]) -> str:
    # Same material serialization as the canonical InternalWorkService producer.
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def stamp(row: object | None, *fields: str) -> dict[str, object] | None:
    if row is None:
        return None
    result: dict[str, object] = {}
    for name in ("id", "version", *fields):
        value = getattr(row, name)
        result[name] = (
            value.isoformat()
            if isinstance(value, datetime)
            else str(value)
            if isinstance(value, UUID)
            else value
        )
    return result


class HumanApprovalService:
    def __init__(self, db: Session, *, at: datetime | None = None):
        self.db = db
        self.at = at or now()

    def _authorize(self, ctx: TenantContext, permission: Permission) -> None:
        if not isinstance(ctx, TenantContext):
            raise HTTPException(403, "HUMAN_REQUIRED")
        IdentityService(self.db).authorize(ctx, permission)

    def _request(self, ctx: TenantContext, request_id: UUID) -> ApprovalRequest:
        # Match worker Tenant -> Run -> Approval ordering before the decision lock.
        peek = self.db.scalar(
            select(ApprovalRequest).where(
                ApprovalRequest.tenant_id == ctx.tenant_id, ApprovalRequest.id == request_id
            )
        )
        if peek is not None and peek.proposal.get("effect") in {"internal_task", "agent_email"}:
            from aco.features.agent.models import AgentRun

            self.db.scalar(select(Tenant).where(Tenant.id == ctx.tenant_id).with_for_update(key_share=True))
            self.db.scalar(
                select(AgentRun)
                .where(AgentRun.tenant_id == ctx.tenant_id, AgentRun.id == UUID(str(peek.proposal["run_id"])))
                .with_for_update()
            )
        if peek is not None and peek.proposal.get("effect") == "voice_session":
            from aco.features.voice.models import VoiceSession

            self.db.scalar(
                select(VoiceSession)
                .where(
                    VoiceSession.tenant_id == ctx.tenant_id,
                    VoiceSession.id == UUID(str(peek.proposal["session_id"])),
                )
                .with_for_update()
            )
        row = self.db.scalar(
            select(ApprovalRequest)
            .where(
                ApprovalRequest.tenant_id == ctx.tenant_id,
                ApprovalRequest.id == request_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise HTTPException(404, "APPROVAL_NOT_FOUND")
        return row

    def _member(self, tenant_id: UUID, user_id: UUID) -> Membership | None:
        return self.db.scalar(
            select(Membership)
            .where(
                Membership.tenant_id == tenant_id,
                Membership.user_id == user_id,
            )
            .execution_options(populate_existing=True)
        )

    def _source_scope(
        self, row: ApprovalRequest
    ) -> tuple[
        ApprovalProposal | InternalTaskApprovalProposal | VoiceApprovalProposal,
        ScheduledAction,
        InvoiceSnapshot,
        dict[str, object],
        builtins.list[str],
    ]:
        if row.proposal.get("effect") == "voice_session":
            from aco.features.voice.service import voice_scope

            return voice_scope(self.db, row, at=self.at)
        if row.proposal.get("effect") == "internal_task":
            from aco.features.review.agent_approvals import internal_scope

            return internal_scope(self.db, row)
        try:
            proposal = (
                AgentEmailApprovalProposal
                if row.proposal.get("effect") == "agent_email"
                else ApprovalProposal
            ).model_validate_json(json.dumps(row.proposal))
        except ValidationError as exc:
            raise HTTPException(409, "INVALID_STORED_PROPOSAL") from exc
        action = self.db.get(ScheduledAction, row.action_id, populate_existing=True)
        if action is None or action.tenant_id != row.tenant_id:
            raise HTTPException(409, "INVALID_APPROVAL_ACTION")
        invoice = self.db.get(InvoiceSnapshot, action.invoice_id, populate_existing=True)
        if invoice is None or invoice.tenant_id != row.tenant_id:
            raise HTTPException(409, "INVALID_APPROVAL_INVOICE")
        reasons = []
        if (
            proposal.action_id != action.id
            or proposal.action_version != row.action_version
            or action.version != row.action_version
            or proposal.policy_version_id != action.policy_version_id
            or digest(row.proposal) != row.material_hash
        ):
            reasons.append("APPROVAL_MATERIAL_CHANGED")
        if len(set(proposal.invoice_ids)) != len(proposal.invoice_ids) or set(proposal.invoice_ids) != {
            invoice.id
        }:
            reasons.append("APPROVAL_INVOICE_SCOPE_CHANGED")
        if action.source_revision != invoice.source_revision:
            reasons.append("APPROVAL_SOURCE_CHANGED")
        if action.status in {"cancelled", "failed", "dead_letter", "succeeded", "unknown_outcome"}:
            reasons.append("ACTION_NOT_REVIEWABLE")
        if invoice.balance_minor <= 0:
            reasons.append("INVOICE_NOT_OUTSTANDING")
        evidence = []
        for evidence_id in proposal.evidence_ids:
            event = self.db.get(AccountingEvent, evidence_id, populate_existing=True)
            if (
                event is None
                or event.tenant_id != row.tenant_id
                or event.invoice_id != invoice.id
                or event.provider_connection_id != invoice.provider_connection_id
            ):
                reasons.append("APPROVAL_EVIDENCE_SCOPE_CHANGED")
            evidence.append(
                stamp(
                    event, "invoice_id", "provider_connection_id", "source_revision", "origin", "provenance"
                )
            )
        policy_version = self.db.get(CollectionPolicyVersion, proposal.policy_version_id)
        policy = (
            self.db.get(CollectionPolicy, policy_version.policy_id, populate_existing=True)
            if policy_version
            else None
        )
        if policy is None or policy.tenant_id != row.tenant_id or policy.archived_at is not None:
            reasons.append("APPROVAL_POLICY_CHANGED")
        versions = list(
            self.db.scalars(
                select(CollectionPolicyVersion.id)
                .where(
                    CollectionPolicyVersion.tenant_id == row.tenant_id,
                    CollectionPolicyVersion.policy_id == policy.id if policy else false(),
                )
                .order_by(CollectionPolicyVersion.version.desc())
                .limit(1)
            )
        )
        if versions != [proposal.policy_version_id]:
            reasons.append("APPROVAL_POLICY_CHANGED")
        contact = self.db.scalar(
            select(CustomerContact)
            .where(
                CustomerContact.tenant_id == row.tenant_id,
                CustomerContact.customer_id == invoice.customer_id,
            )
            .execution_options(populate_existing=True)
        )
        if proposal.channel != "email" or contact is None or contact.email != proposal.destination:
            reasons.append("APPROVAL_DESTINATION_CHANGED_OR_UNSUPPORTED")
        control = self.db.scalar(
            select(RecipientControl)
            .where(
                RecipientControl.tenant_id == row.tenant_id,
                RecipientControl.contact_id == contact.id if contact else false(),
            )
            .execution_options(populate_existing=True)
        )
        if control is not None and (control.opt_out or proposal.channel not in control.allowed_channels):
            reasons.append("RECIPIENT_RESTRICTED")
        if proposal.consent_mode != "no_capture":
            reasons.append("CAPTURE_APPROVAL_UNAVAILABLE")
        collection = self.db.scalar(
            select(CollectionRecord)
            .where(
                CollectionRecord.tenant_id == row.tenant_id,
                CollectionRecord.invoice_id == invoice.id,
            )
            .execution_options(populate_existing=True)
        )
        holds = list(
            self.db.scalars(
                select(CollectionHold)
                .where(
                    CollectionHold.tenant_id == row.tenant_id,
                    CollectionHold.invoice_id == invoice.id,
                    CollectionHold.released_at.is_(None),
                )
                .order_by(CollectionHold.id)
                .execution_options(populate_existing=True)
            )
        )
        reviews = list(
            self.db.scalars(
                select(CustomerScopeReview)
                .where(
                    CustomerScopeReview.tenant_id == row.tenant_id,
                    CustomerScopeReview.provider_connection_id == invoice.provider_connection_id,
                    CustomerScopeReview.customer_id == invoice.customer_id,
                    CustomerScopeReview.released_at.is_(None),
                )
                .order_by(CustomerScopeReview.id)
                .execution_options(populate_existing=True)
            )
        )
        contact_stops = pending_contact_stops(self.db, invoice)
        if holds or reviews or contact_stops or (collection and collection.hold_reasons):
            reasons.append("COLLECTION_HELD")
        memberships = []
        for initiator_id in sorted({row.initiator_id, action.initiator_id}, key=str):
            member = self._member(row.tenant_id, initiator_id)
            if (
                member is None
                or not member.active
                or Permission.TASK_WRITE not in ROLE_PERMISSIONS[Role(member.role)]
            ):
                reasons.append("INITIATOR_PERMISSION_CHANGED")
            memberships.append(stamp(member, "user_id", "role", "active"))
        action_member = self._member(row.tenant_id, action.initiator_id)
        if action_member is None or action.membership_version != action_member.version:
            reasons.append("INITIATOR_PERMISSION_CHANGED")
        proposer = self.db.get(Actor, row.proposer_id, populate_existing=True)
        if (
            proposer is None
            or not proposer.active
            or (proposer.kind == "service" and proposer.tenant_id != row.tenant_id)
        ):
            reasons.append("PROPOSER_UNAVAILABLE")
        tenant = self.db.get(Tenant, row.tenant_id, populate_existing=True)
        connection = self.db.get(ProviderConnection, invoice.provider_connection_id, populate_existing=True)
        binding: dict[str, object] = {
            "request_id": str(row.id),
            "action_id": str(action.id),
            "action_version": action.version,
            "material_hash": row.material_hash,
            "proposal": row.proposal,
            "invoice": stamp(
                invoice,
                "customer_id",
                "provider_connection_id",
                "source_revision",
                "balance_minor",
                "currency",
                "exponent",
                "financial_status",
            ),
            "policy": stamp(policy, "active", "archived_at"),
            "policy_version": stamp(policy_version, "state", "definition"),
            "contact": stamp(contact, "email", "source_revision", "verified_for_outreach"),
            "recipient": stamp(
                control,
                "reviewed_destination",
                "reviewed_contact_revision",
                "destination_verified",
                "timezone",
                "timezone_verified",
                "owner_confirmed_tenant_timezone",
                "confirmed_tenant_timezone",
                "allowed_channels",
                "opt_out",
                "evidence",
            ),
            "evidence": evidence,
            "initiators": memberships,
            "proposer": stamp(proposer, "kind", "active"),
            "collection": stamp(collection, "lifecycle", "hold_reasons"),
            "holds": [stamp(h, "reason", "expires_at", "released_at") for h in holds],
            "scope_reviews": [stamp(r, "released_at", "expires_at") for r in reviews],
            "customer_contact_stops": contact_stops,
            "tenant": stamp(tenant, "outbound_paused", "automation_enabled", "timezone"),
            "connection": stamp(connection, "status"),
            "reservation_id": str(action.reservation_id) if action.reservation_id else None,
        }
        if isinstance(proposal, AgentEmailApprovalProposal):
            from aco.features.review.email_approvals import email_scope

            binding["agent_email"], email_reasons = email_scope(self.db, row, proposal, action, invoice)
            reasons.extend(email_reasons)
        return proposal, action, invoice, binding, sorted(set(reasons))

    def _scope(
        self, row: ApprovalRequest
    ) -> tuple[
        ApprovalProposal | InternalTaskApprovalProposal | VoiceApprovalProposal,
        ScheduledAction,
        InvoiceSnapshot,
        dict[str, object],
        builtins.list[str],
    ]:
        from aco.features.review.modifications import lineage, replacement

        proposal, action, invoice, binding, reasons = self._source_scope(row)
        history, _, history_reasons = lineage(self.db, row)
        if history:
            binding["modifications"] = history
        reasons.extend(history_reasons)
        if replacement(self.db, row) is not None:
            reasons.append("APPROVAL_SUPERSEDED")
        return proposal, action, invoice, binding, sorted(set(reasons))

    def _grant_reasons(
        self, row: ApprovalRequest, binding: dict[str, object], action: ScheduledAction
    ) -> builtins.list[str]:
        from aco.features.review.modifications import lineage

        _, editors, _ = lineage(self.db, row)
        grants = list(
            self.db.scalars(
                select(ApprovalDecision).where(
                    ApprovalDecision.tenant_id == row.tenant_id,
                    ApprovalDecision.request_id == row.id,
                    ApprovalDecision.decision == "approved",
                )
            )
        )
        granted = grants[0] if len(grants) == 1 else None
        member = self._member(row.tenant_id, granted.approver_id) if granted else None
        approving_actor = self.db.get(Actor, granted.approver_id, populate_existing=True) if granted else None
        reasons = []
        if (
            approving_actor is None
            or not approving_actor.active
            or granted is None
            or granted.review_binding is None
            or granted.review_binding.get("scope") != binding
            or granted.material_hash != row.material_hash
            or granted.approver_id in {row.proposer_id, row.initiator_id, action.initiator_id, *editors}
            or member is None
            or not member.active
            or Permission.APPROVE_EXTERNAL not in ROLE_PERMISSIONS[Role(member.role)]
            or granted.review_binding.get("approver_membership") != stamp(member, "user_id", "role", "active")
        ):
            reasons.append("APPROVAL_REVIEWED_STATE_CHANGED")
        if (
            row.expires_at is None
            or row.granted_at is None
            or self.at >= row.expires_at
            or row.expires_at > row.granted_at + timedelta(hours=24)
            or self.at < row.granted_at
        ):
            reasons.append("APPROVAL_EXPIRED")
        return reasons

    def _view(self, ctx: TenantContext, row: ApprovalRequest) -> ApprovalView:
        proposal, action, invoice, binding, reasons = self._scope(row)
        decisions = list(
            self.db.scalars(
                select(ApprovalDecision)
                .where(
                    ApprovalDecision.tenant_id == ctx.tenant_id,
                    ApprovalDecision.request_id == row.id,
                )
                .order_by(ApprovalDecision.created_at, ApprovalDecision.id)
            )
        )
        member = self._member(ctx.tenant_id, ctx.actor_id)
        authorized = (
            member is not None
            and member.active
            and Permission.APPROVE_EXTERNAL in ROLE_PERMISSIONS[Role(member.role)]
        )
        from aco.features.review.modifications import lineage, modification_reasons, replacement

        _, editors, _ = lineage(self.db, row)
        successor = replacement(self.db, row)
        edit_reasons = modification_reasons(self.db, row, action, reasons, authorized)
        independent = ctx.actor_id not in {row.proposer_id, row.initiator_id, action.initiator_id, *editors}
        effective = "pending"
        if row.status == "denied":
            effective = "denied"
        elif row.consumed_at is not None:
            effective = "consumed"
        elif row.status == "approved":
            effective = "approved"
            reasons.extend(self._grant_reasons(row, binding, action))
            if "APPROVAL_EXPIRED" in reasons:
                effective = "expired"
        elif row.status == "superseded":
            effective = "stale"
        elif row.status != "pending":
            reasons.append("INVALID_APPROVAL_STATUS")
        if reasons and effective in {"pending", "approved"}:
            effective = "stale"
        can_approve = row.status == "pending" and not reasons and authorized and independent
        can_deny = row.status == "pending" and authorized and successor is None
        if row.status == "pending" and not independent:
            reasons.append("self_approval_prohibited")
        if not authorized:
            reasons.append("APPROVER_PERMISSION_REQUIRED")
        changed = (action.result or {}).get("changed_ids", [])
        changed_ids = [UUID(str(key)) for key in changed] if isinstance(changed, list) else []
        return ApprovalView.model_validate(
            {
                "id": row.id,
                "version": row.version,
                "action_id": row.action_id,
                "action_version": row.action_version,
                "proposer_id": row.proposer_id,
                "initiator_id": row.initiator_id,
                "source_action_initiator_id": action.initiator_id,
                "customer_id": invoice.customer_id,
                "material_hash": row.material_hash,
                "current_binding_hash": digest(binding),
                "proposal": proposal,
                "invoice_scope": ApprovalInvoiceScope(
                    invoice_id=invoice.id,
                    number=invoice.number,
                    customer_id=invoice.customer_id,
                    source_revision=invoice.source_revision,
                    outstanding=MoneyJSON(
                        minor_units=invoice.balance_minor,
                        currency=invoice.currency,
                        exponent=invoice.exponent,
                    ),
                    financial_status=invoice.financial_status,
                    observed_at=invoice.observed_at,
                    synthetic=invoice.synthetic,
                    verified=invoice.verified,
                ),
                "evidence": [
                    ApprovalEvidenceView(
                        id=e.id,
                        invoice_id=e.invoice_id,
                        source_revision=e.source_revision,
                        origin=e.origin,
                        source_json=json.dumps(
                            {"envelope": e.envelope, "provenance": e.provenance, "result": e.result},
                            sort_keys=True,
                        ),
                    )
                    for e in self.db.scalars(
                        select(AccountingEvent)
                        .where(
                            AccountingEvent.tenant_id == ctx.tenant_id,
                            AccountingEvent.invoice_id == invoice.id,
                            AccountingEvent.id.in_(
                                proposal.evidence_ids if isinstance(proposal, ApprovalProposal) else []
                            ),
                        )
                        .order_by(AccountingEvent.id)
                    )
                ],
                "effect": proposal.effect
                if isinstance(proposal, (InternalTaskApprovalProposal, VoiceApprovalProposal))
                else "outbound_message",
                "source_run_id": action.agent_run_id,
                "source_step_id": action.agent_step_id,
                "agent_evidence": self._agent_evidence(row, proposal),
                "changed_ids": changed_ids,
                "execution_status": (
                    "executed"
                    if row.consumed_at and action.status == "succeeded"
                    else "denied"
                    if row.status == "denied"
                    else "stale"
                    if effective in {"stale", "expired"}
                    else "failed"
                    if action.status in {"failed", "cancelled"}
                    else "queued"
                    if row.status == "approved"
                    else "pending_approval"
                ),
                "dispatch_status": "NOT_APPLICABLE"
                if isinstance(proposal, (InternalTaskApprovalProposal, VoiceApprovalProposal))
                else (
                    "ACCEPTED"
                    if action.status == "succeeded"
                    else "UNKNOWN"
                    if action.status == "unknown_outcome"
                    else "STOPPED"
                    if action.status in {"failed", "cancelled", "dead_letter"}
                    else "QUEUED"
                    if action.status in {"queued", "executing"}
                    else "AWAITING_REVIEW"
                )
                if isinstance(proposal, AgentEmailApprovalProposal)
                else "NOT_ENABLED",
                "status": row.status,
                "effective_status": effective,
                "approval_valid": effective == "approved",
                "new_approval_required": effective in {"stale", "expired", "consumed"},
                "can_modify": not edit_reasons,
                "modification_reasons": edit_reasons,
                "supersedes_id": row.supersedes_id,
                "replacement_id": successor.id if successor else None,
                "editor_id": row.editor_id,
                "edit_reason": row.edit_reason,
                "can_approve": can_approve,
                "can_deny": can_deny,
                "reasons": sorted(set(reasons)),
                "granted_at": row.granted_at,
                "expires_at": row.expires_at,
                "consumed_at": row.consumed_at,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "decisions": [
                    ApprovalDecisionView(
                        id=d.id,
                        approver_id=d.approver_id,
                        decision=d.decision,
                        reason=d.reason,
                        material_hash=d.material_hash,
                        created_at=d.created_at,
                    )
                    for d in decisions
                ],
            }
        )

    def _agent_evidence(
        self,
        row: ApprovalRequest,
        proposal: ApprovalProposal | InternalTaskApprovalProposal | VoiceApprovalProposal,
    ) -> builtins.list[AgentApprovalEvidence]:
        if not isinstance(proposal, (InternalTaskApprovalProposal, AgentEmailApprovalProposal)):
            return []
        from aco.features.agent.models import AgentStep

        return [
            AgentApprovalEvidence(
                step_id=s.id,
                run_id=s.run_id,
                tool_name=s.tool_name or "",
                source_json=json.dumps(s.output, sort_keys=True),
            )
            for s in self.db.scalars(
                select(AgentStep)
                .where(
                    AgentStep.tenant_id == row.tenant_id,
                    AgentStep.run_id == proposal.run_id,
                    AgentStep.id.in_(proposal.evidence_step_ids),
                )
                .order_by(AgentStep.id)
            )
        ]

    def get(self, ctx: TenantContext, request_id: UUID) -> ApprovalView:
        self._authorize(ctx, Permission.INVOICE_READ)
        return self._view(ctx, self._request(ctx, request_id))

    def list(
        self, ctx: TenantContext, invoice_id: UUID | None = None, action_id: UUID | None = None
    ) -> ApprovalPage:
        self._authorize(ctx, Permission.INVOICE_READ)
        query = (
            select(ApprovalRequest)
            .join(ScheduledAction, ScheduledAction.id == ApprovalRequest.action_id)
            .where(ApprovalRequest.tenant_id == ctx.tenant_id, ScheduledAction.tenant_id == ctx.tenant_id)
        )
        if invoice_id is not None:
            query = query.where(ScheduledAction.invoice_id == invoice_id)
        if action_id is not None:
            query = query.where(ApprovalRequest.action_id == action_id)
        rows = self.db.scalars(
            query.order_by(ApprovalRequest.created_at.desc(), ApprovalRequest.id)
            .limit(100)
            .execution_options(populate_existing=True)
        )
        return ApprovalPage(items=[self._view(ctx, row) for row in rows])

    def decide_approval(
        self, ctx: TenantContext, request_id: UUID, command: ApprovalDecisionCommand
    ) -> ApprovalView:
        self._authorize(ctx, Permission.APPROVE_EXTERNAL)
        row = self._request(ctx, request_id)
        view = self._view(ctx, row)
        if row.version != command.expected_version or row.status != "pending":
            raise HTTPException(409, "STALE_APPROVAL")
        if (
            view.action_version != command.reviewed_action_version
            or row.material_hash != command.reviewed_material_hash
            or view.current_binding_hash != command.reviewed_binding_hash
        ):
            raise HTTPException(409, "APPROVAL_REVIEW_CHANGED")
        if command.decision == "approved":
            if "self_approval_prohibited" in view.reasons:
                raise HTTPException(403, "self_approval_prohibited")
            if not view.can_approve:
                raise HTTPException(409, "APPROVAL_SCOPE_INVALID")
        if not command.reason.strip():
            raise HTTPException(422, "APPROVAL_REASON_REQUIRED")
        _, _, _, binding, _ = self._scope(row)
        member = self._member(ctx.tenant_id, ctx.actor_id)
        assert member is not None
        decision = ApprovalDecision(
            tenant_id=ctx.tenant_id,
            request_id=row.id,
            approver_id=ctx.actor_id,
            decision=command.decision,
            reason=command.reason.strip(),
            material_hash=row.material_hash,
            review_binding={
                "scope": binding,
                "approver_membership": stamp(member, "user_id", "role", "active"),
            },
            created_at=self.at,
            updated_at=self.at,
        )
        self.db.add(decision)
        row.status = command.decision
        row.version += 1
        row.updated_at = self.at
        if command.decision == "approved":
            row.granted_at = self.at
            row.expires_at = self.at + timedelta(seconds=command.expires_in_seconds)
        self.db.flush()
        IdentityService(self.db).audit(
            ctx,
            "approval." + command.decision,
            row.id,
            {
                "decision_id": str(decision.id),
                "material_hash": row.material_hash,
                "scope_hash": digest(binding),
                "dispatch_authorized": False,
            },
        )
        if row.proposal.get("effect") in {"internal_task", "agent_email"}:
            from aco.features.agent.models import AgentRun
            from aco.features.review.agent_approvals import queue_decided_run

            run = self.db.get(AgentRun, UUID(str(row.proposal["run_id"])), with_for_update=True)
            if run is not None and run.tenant_id == ctx.tenant_id:
                queue_decided_run(self.db, run)
        return self._view(ctx, row)
