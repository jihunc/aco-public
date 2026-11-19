"""Exact internal-task approval effect; no human impersonation or outbound authority."""

import json
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.types import Role
from aco.features.accounting.models import InvoiceSnapshot
from aco.features.actions.models import ScheduledAction
from aco.features.agent.context import RunAuthority, current_run, selected_invoices
from aco.features.agent.events import append_event
from aco.features.agent.models import AgentRun, AgentStep
from aco.features.cases.task_binding import current_task_case
from aco.features.identity.models import Actor, AuditEvent, Membership, ProviderConnection, Tenant, now
from aco.features.identity.service import ROLE_PERMISSIONS, Permission
from aco.features.review.approval_dto import InternalTaskApprovalProposal
from aco.features.review.models import ApprovalRequest
from aco.features.tools.read_names import canonical_read_name

if TYPE_CHECKING:
    from aco.features.tools.gateway import CreateTaskCommand


def internal_scope(
    db: Session, row: ApprovalRequest
) -> tuple[InternalTaskApprovalProposal, ScheduledAction, InvoiceSnapshot, dict[str, object], list[str]]:
    from aco.features.review.approvals import digest, stamp

    proposal = InternalTaskApprovalProposal.model_validate_json(json.dumps(row.proposal))
    action = db.get(ScheduledAction, row.action_id, populate_existing=True)
    if action is None or action.tenant_id != row.tenant_id:
        raise HTTPException(409, "INVALID_APPROVAL_ACTION")
    run = db.get(AgentRun, proposal.run_id, populate_existing=True)
    invoice = db.get(InvoiceSnapshot, action.invoice_id, populate_existing=True)
    if run is None or invoice is None or run.tenant_id != row.tenant_id or invoice.tenant_id != row.tenant_id:
        raise HTTPException(409, "INVALID_APPROVAL_RUN_SCOPE")
    reasons: list[str] = []
    payload: dict[str, object] = {
        "invoice_id": str(invoice.id),
        "title": proposal.title,
        "notes": proposal.notes,
        "evidence_step_ids": [str(k) for k in proposal.evidence_step_ids],
    }
    if proposal.case_binding is not None:
        payload["case_binding"] = proposal.case_binding.model_dump(mode="json")
    material = {"kind": "create_collection_task", "payload": payload}
    step = db.get(AgentStep, action.agent_step_id, populate_existing=True)
    if (
        proposal.action_id != action.id
        or proposal.action_version != action.version
        or row.action_version != action.version
        or digest(row.proposal) != row.material_hash
        or action.command != material
        or action.kind != "agent_internal"
        or action.agent_run_id != run.id
        or action.initiator_id != run.initiator_id
        or row.initiator_id != run.initiator_id
        or row.proposer_id != run.worker_actor_id
        or action.worker_actor_id != run.worker_actor_id
        or step is None
        or step.tenant_id != row.tenant_id
        or step.run_id != run.id
        or step.kind != "action"
        or step.input != material["payload"]
    ):
        reasons.append("APPROVAL_MATERIAL_CHANGED")
    if proposal.invoice_ids != [invoice.id] or invoice.id not in selected_invoices(db, run):
        reasons.append("APPROVAL_INVOICE_SCOPE_CHANGED")
    if not invoice.verified or action.source_revision != invoice.source_revision:
        reasons.append("APPROVAL_SOURCE_CHANGED")
    if run.status in {"failed", "cancelled"} or action.status in {"cancelled", "failed", "unknown_outcome"}:
        reasons.append("ACTION_NOT_REVIEWABLE")
    reads = list(
        db.scalars(
            select(AgentStep)
            .where(
                AgentStep.tenant_id == row.tenant_id,
                AgentStep.run_id == run.id,
                AgentStep.invoice_id == invoice.id,
                AgentStep.id.in_(proposal.evidence_step_ids),
                AgentStep.kind == "read",
                AgentStep.status == "completed",
            )
            .order_by(AgentStep.id)
        )
    )
    if len(reads) != len(set(proposal.evidence_step_ids)) or not {
        "get_invoice",
        "get_verified_payments",
    }.issubset({canonical_read_name(s.tool_name) for s in reads}):
        reasons.append("APPROVAL_EVIDENCE_SCOPE_CHANGED")
    member = db.scalar(
        select(Membership)
        .where(Membership.tenant_id == row.tenant_id, Membership.user_id == run.initiator_id)
        .execution_options(populate_existing=True)
    )
    if (
        member is None
        or not member.active
        or member.version != run.membership_version
        or action.membership_version != member.version
        or Permission.INVESTIGATE not in ROLE_PERMISSIONS[Role(member.role)]
    ):
        reasons.append("INITIATOR_PERMISSION_CHANGED")
    actor = db.get(Actor, run.worker_actor_id, populate_existing=True)
    if (
        actor is None
        or not actor.active
        or actor.kind != "service"
        or actor.tenant_id != row.tenant_id
        or actor.version != run.actor_version
    ):
        reasons.append("PROPOSER_UNAVAILABLE")
    tenant = db.get(Tenant, row.tenant_id, populate_existing=True)
    connection = db.get(ProviderConnection, invoice.provider_connection_id, populate_existing=True)
    model_connection = db.get(ProviderConnection, run.provider_connection_id, populate_existing=True)
    if tenant is None or tenant.version != run.tenant_generation:
        reasons.append("APPROVAL_WORKSPACE_CHANGED")
    if (
        connection is None
        or connection.tenant_id != row.tenant_id
        or connection.status != "READY"
        or connection.version != action.connection_version
        or action.provider_connection_id != invoice.provider_connection_id
        or model_connection is None
        or model_connection.tenant_id != row.tenant_id
        or model_connection.status != "READY"
        or model_connection.version != run.connection_version
    ):
        reasons.append("APPROVAL_CONNECTION_CHANGED")
    binding: dict[str, object] = {
        "effect": "internal_task",
        "request_id": str(row.id),
        "material_hash": row.material_hash,
        "proposal": row.proposal,
        "command": action.command,
        "run": {
            "id": str(run.id),
            "initiator_id": str(run.initiator_id),
            "worker_actor_id": str(run.worker_actor_id),
            "membership_version": run.membership_version,
            "actor_version": run.actor_version,
            "tenant_generation": run.tenant_generation,
            "invoice_ids": sorted(map(str, selected_invoices(db, run))),
        },
        "invoice": stamp(
            invoice,
            "customer_id",
            "provider_connection_id",
            "source_revision",
            "balance_minor",
            "currency",
            "exponent",
            "financial_status",
            "verified",
        ),
        "evidence": [
            stamp(s, "run_id", "invoice_id", "kind", "tool_name", "status", "input", "output") for s in reads
        ],
        "initiator": stamp(member, "user_id", "role", "active"),
        "proposer": stamp(actor, "kind", "active"),
        "tenant": stamp(tenant, "outbound_paused", "automation_enabled"),
        "connection": stamp(connection, "status"),
        "model_connection": stamp(model_connection, "status"),
    }
    if proposal.case_binding is not None:
        try:
            binding["case_binding"] = current_task_case(
                db, run, invoice.id, proposal.case_binding, proposal.evidence_step_ids
            )
        except HTTPException as error:
            reasons.append(str(error.detail))
    return proposal, action, invoice, binding, sorted(set(reasons))


def queue_decided_run(db: Session, run: AgentRun) -> None:
    """Existing run dispatcher is the durable resume outbox; decisions are never commands."""
    if run.status != "awaiting_approval":
        return
    pending = db.scalar(
        select(ApprovalRequest.id)
        .join(ScheduledAction, ScheduledAction.id == ApprovalRequest.action_id)
        .where(
            ApprovalRequest.tenant_id == run.tenant_id,
            ScheduledAction.agent_run_id == run.id,
            ApprovalRequest.status == "pending",
        )
        .limit(1)
    )
    if pending is not None:
        return
    run.status, run.job_id = "queued", None
    run.job_version += 1
    run.version += 1
    run.lease_token, run.lease_until = None, None
    append_event(
        db,
        run,
        "approval.resume_queued",
        "Human decisions recorded; current guarded execution will revalidate them",
    )


def task_approval(
    db: Session, authority: RunAuthority, action: ScheduledAction, command: "CreateTaskCommand"
) -> tuple[UUID, str]:
    from aco.features.review.approvals import HumanApprovalService, digest

    run = current_run(db, authority)
    row = db.scalar(
        select(ApprovalRequest)
        .where(ApprovalRequest.tenant_id == run.tenant_id, ApprovalRequest.action_id == action.id)
        .with_for_update()
    )
    if row is None:
        proposal = InternalTaskApprovalProposal(
            case_binding=command.case_binding,
            action_id=action.id,
            action_version=action.version,
            run_id=run.id,
            invoice_ids=[command.invoice_id],
            evidence_step_ids=command.evidence_step_ids,
            title=command.title,
            notes=command.notes,
        )
        payload = proposal.model_dump(mode="json")
        row = ApprovalRequest(
            tenant_id=run.tenant_id,
            action_id=action.id,
            action_version=action.version,
            proposer_id=run.worker_actor_id,
            initiator_id=run.initiator_id,
            material_hash=digest(payload),
            proposal=payload,
        )
        db.add(row)
        action.status, action.attempts = "awaiting_approval", 0
        action.job_id, action.lease_token, action.lease_until = None, None, None
        db.flush()
        append_event(
            db,
            run,
            "approval.requested",
            "Independent human approval required before this internal task",
            action.agent_step_id,
        )
        db.add(
            AuditEvent(
                tenant_id=run.tenant_id,
                actor_id=run.worker_actor_id,
                correlation_id=run.correlation_id,
                action="approval.requested",
                target_id=row.id,
                details={
                    "effect": "internal_task",
                    "run_id": str(run.id),
                    "action_id": str(action.id),
                    "material_hash": row.material_hash,
                },
            )
        )
        return row.id, "pending"
    if row.status == "pending":
        return row.id, "pending"
    if row.status == "denied":
        return row.id, "denied"
    service = HumanApprovalService(db)
    _, checked, _, binding, reasons = service._scope(row)
    reasons.extend(service._grant_reasons(row, binding, checked))
    if reasons or row.consumed_at is not None or row.status != "approved":
        return row.id, "stale"
    row.consumed_at = now()
    row.version += 1
    db.add(
        AuditEvent(
            tenant_id=run.tenant_id,
            actor_id=run.worker_actor_id,
            correlation_id=run.correlation_id,
            action="approval.consumed",
            target_id=row.id,
            details={
                "effect": "internal_task",
                "run_id": str(run.id),
                "action_id": str(action.id),
                "material_hash": row.material_hash,
            },
        )
    )
    append_event(
        db, run, "approval.consumed", "Exact approval consumed for internal task only", action.agent_step_id
    )
    return row.id, "approved"
