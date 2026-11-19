"""Additional exact run/read/activation bindings for canonical email approvals."""

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.types import Role
from aco.features.accounting.models import InvoiceSnapshot
from aco.features.actions.models import ScheduledAction
from aco.features.agent.context import selected_invoices
from aco.features.agent.models import AgentRun, AgentStep
from aco.features.communications.activation import latest, validate_activation
from aco.features.identity.models import Actor, Membership, ProviderConnection, Tenant
from aco.features.identity.service import ROLE_PERMISSIONS, Permission
from aco.features.review.email_cost import live_email_cost_scope
from aco.features.review.email_dto import AgentEmailApprovalProposal
from aco.features.review.models import ApprovalRequest
from aco.features.tools.email_dto import DraftCustomerEmail


def email_scope(
    db: Session,
    row: ApprovalRequest,
    proposal: AgentEmailApprovalProposal,
    action: ScheduledAction,
    invoice: InvoiceSnapshot,
) -> tuple[dict[str, object], list[str]]:
    from aco.features.review.approvals import stamp
    from aco.features.tools.email import email_evidence

    run = db.get(AgentRun, proposal.run_id, populate_existing=True)
    tenant = db.get(Tenant, row.tenant_id, populate_existing=True)
    if run is None or run.tenant_id != row.tenant_id or tenant is None:
        raise HTTPException(409, "INVALID_APPROVAL_RUN_SCOPE")
    draft = DraftCustomerEmail(invoice_id=invoice.id, subject=proposal.subject, content=proposal.content)
    material = {
        "kind": "request_email_send",
        "payload": draft.model_dump(mode="json"),
        "evidence_step_ids": list(map(str, proposal.evidence_step_ids)),
        "rule_id": str(proposal.rule_id),
        "destination": proposal.destination,
        "email_activation_id": str(proposal.email_activation_id),
        "channel": "email",
    }
    step = db.get(AgentStep, action.agent_step_id, populate_existing=True)
    reasons: list[str] = []
    if (
        action.kind != "agent_email"
        or action.command != material
        or action.agent_run_id != run.id
        or action.initiator_id != run.initiator_id
        or row.initiator_id != run.initiator_id
        or row.proposer_id != run.worker_actor_id
        or action.worker_actor_id != run.worker_actor_id
        or step is None
        or step.tenant_id != row.tenant_id
        or step.run_id != run.id
        or step.invoice_id != invoice.id
        or step.input != draft.model_dump(mode="json")
    ):
        reasons.append("APPROVAL_MATERIAL_CHANGED")
    if run.status in {"failed", "cancelled"}:
        reasons.append("ACTION_NOT_REVIEWABLE")
    reads: list[AgentStep] = []
    try:
        reads = email_evidence(db, run, invoice, proposal.evidence_step_ids)
    except HTTPException as error:
        reasons.append(str(error.detail))
    member = db.scalar(
        select(Membership)
        .where(Membership.tenant_id == row.tenant_id, Membership.user_id == run.initiator_id)
        .execution_options(populate_existing=True)
    )
    human = db.get(Actor, run.initiator_id, populate_existing=True)
    if human is None or not human.active or human.kind != "human" or human.user_id != run.initiator_id:
        reasons.append("INITIATOR_PERMISSION_CHANGED")
    actor = db.get(Actor, run.worker_actor_id, populate_existing=True)
    model = db.get(ProviderConnection, run.provider_connection_id, populate_existing=True)
    if (
        member is None
        or not member.active
        or member.version != run.membership_version
        or Permission.INVESTIGATE not in ROLE_PERMISSIONS[Role(member.role)]
    ):
        reasons.append("INITIATOR_PERMISSION_CHANGED")
    if (
        actor is None
        or not actor.active
        or actor.kind != "service"
        or actor.tenant_id != row.tenant_id
        or actor.version != run.actor_version
    ):
        reasons.append("PROPOSER_UNAVAILABLE")
    if (
        model is None
        or model.tenant_id != row.tenant_id
        or model.status != "READY"
        or model.version != run.connection_version
    ):
        reasons.append("APPROVAL_CONNECTION_CHANGED")
    if tenant.version != run.tenant_generation:
        reasons.append("APPROVAL_WORKSPACE_CHANGED")
    activation = latest(db, row.tenant_id)
    if activation is None or activation.id != proposal.email_activation_id:
        reasons.append("EMAIL_EXECUTION_REVIEW_REQUIRED")
    else:
        try:
            validate_activation(db, tenant, activation)
        except HTTPException as error:
            reasons.append(str(error.detail))
        try:
            cost = live_email_cost_scope(db, activation)
            if (
                proposal.live_cost_scope != cost
                or proposal.cost_basis
                != ("SHARED_GRANT_AUTHORIZATION_CEILING" if cost else "SYNTHETIC_NO_CHARGE")
                or proposal.cost_bound != (cost.authorization_ceiling if cost else "0")
                or proposal.currency != ("USD" if activation.provider_binding is not None else "AUD")
            ):
                reasons.append("EMAIL_APPROVAL_COST_SCOPE_CHANGED")
        except HTTPException as error:
            reasons.append(str(error.detail))
        if proposal.destination not in activation.destinations:
            reasons.append("APPROVAL_DESTINATION_CHANGED_OR_UNSUPPORTED")
    return {
        "run_id": str(run.id),
        "selected_invoices": sorted(map(str, selected_invoices(db, run))),
        "command": action.command,
        "evidence": [
            stamp(s, "run_id", "invoice_id", "kind", "tool_name", "status", "input", "output") for s in reads
        ],
        "model_connection": stamp(model, "status"),
        "initiating_actor": stamp(human, "active", "kind"),
        "activation": stamp(
            activation,
            "email_connection_id",
            "email_connection_version",
            "storage_connection_id",
            "storage_connection_version",
            "reviewer_id",
            "membership_version",
            "sender",
            "destinations",
            "request_cap",
            "expires_at",
            "provider_binding",
        ),
    }, reasons


def current_email_approval(
    db: Session, action: ScheduledAction, *, allow_consumed: bool = False
) -> ApprovalRequest:
    """Validity only; callers still need the canonical worker, source read and one-use ticket."""
    from aco.features.review.approvals import HumanApprovalService

    row = db.scalar(
        select(ApprovalRequest)
        .where(ApprovalRequest.tenant_id == action.tenant_id, ApprovalRequest.action_id == action.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or row.proposal.get("effect") != "agent_email" or row.status != "approved":
        raise HTTPException(403, "EMAIL_INDEPENDENT_APPROVAL_REQUIRED")
    service = HumanApprovalService(db)
    _, checked, _, binding, reasons = service._scope(row)
    reasons.extend(service._grant_reasons(row, binding, checked))
    if reasons or (row.consumed_at is not None and not allow_consumed):
        raise HTTPException(403, reasons[0] if reasons else "EMAIL_APPROVAL_ALREADY_CONSUMED")
    return row


def validate_email_worker(db: Session, action: ScheduledAction, lease_token: object) -> None:
    """Only the exact queued/active canonical action can use a reviewed agent email."""
    from datetime import timedelta

    from sqlalchemy import text

    from aco.features.actions.models import ActionExecution
    from aco.features.identity.models import now
    from aco.features.policies.guards import PROCESS_INCARNATION
    from aco.features.policies.models import DispatchTicketRecord

    row = current_email_approval(db, action, allow_consumed=action.status == "executing")
    run = db.get(AgentRun, action.agent_run_id, populate_existing=True)
    if run is None or run.status not in {"running", "awaiting_action", "awaiting_approval"}:
        raise HTTPException(403, "EMAIL_RUN_NOT_CURRENT")
    if action.status != "executing":
        return
    attempt = db.scalar(select(ActionExecution).where(ActionExecution.lease_token == lease_token))
    job = db.execute(
        text(
            "SELECT j.status::text, w.last_heartbeat FROM procrastinate_jobs j "
            "LEFT JOIN procrastinate_workers w ON w.id=j.worker_id WHERE j.id=:id"
        ),
        {"id": action.job_id},
    ).first()
    if (
        attempt is None
        or attempt.tenant_id != action.tenant_id
        or attempt.action_id != action.id
        or attempt.attempt != action.attempts
        or attempt.status != "executing"
        or attempt.finished_at is not None
        or job is None
        or job.status != "doing"
        or job.last_heartbeat is None
        or job.last_heartbeat < now() - timedelta(seconds=90)
    ):
        raise HTTPException(403, "EMAIL_EXECUTION_NOT_CURRENT")
    if row.consumed_at is not None:
        ticket = db.scalar(
            select(DispatchTicketRecord).where(
                DispatchTicketRecord.tenant_id == action.tenant_id,
                DispatchTicketRecord.action_id == action.id,
                DispatchTicketRecord.attempt_id == attempt.id,
                DispatchTicketRecord.consumed_at == row.consumed_at,
                DispatchTicketRecord.incarnation == PROCESS_INCARNATION,
            )
        )
        if ticket is None or ticket.bindings.get("approval") != str(row.id):
            raise HTTPException(403, "EMAIL_APPROVAL_CONSUMPTION_OUTSIDE_ATTEMPT")
