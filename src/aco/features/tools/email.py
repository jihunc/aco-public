"""Graph-owned inert email drafting and exact independent-review requests."""

import json
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.types import StrictModel
from aco.features.accounting.models import AccountingEvent, CustomerContact, InvoiceSnapshot
from aco.features.actions.models import ScheduledAction
from aco.features.agent.context import RunAuthority, current_run, selected_invoices
from aco.features.agent.events import append_event, finish_step, start_step
from aco.features.agent.models import AgentRun, AgentStep
from aco.features.communications.activation import latest, validate_activation
from aco.features.identity.models import AuditEvent, ProviderConnection, Tenant, now
from aco.features.policies.dto import PolicyDefinition
from aco.features.policies.models import CollectionPolicy, CollectionPolicyVersion
from aco.features.review.approvals import digest
from aco.features.review.email_cost import live_email_cost_scope
from aco.features.review.email_dto import AgentEmailApprovalProposal
from aco.features.review.models import ApprovalRequest
from aco.features.tools.email_dto import DraftCustomerEmail
from aco.features.tools.gateway import ExecutedAction
from aco.features.tools.read_names import canonical_read_name
from aco.features.tools.schemas import InvoiceFact, ReadEvidence


class DraftResult(StrictModel):
    action_id: None = None
    step_id: UUID
    changed_ids: list[UUID] = Field(default_factory=list)
    status: Literal["drafted"] = "drafted"
    approval_id: None = None


def save_inert_draft(
    db: Session, run: AgentRun, draft: DraftCustomerEmail, index: int, evidence_ids: list[UUID]
) -> DraftResult:
    """Caller holds the current run lock and has revalidated the exact source reads."""
    arguments = draft.model_dump(mode="json")
    output: dict[str, object] = {
        "proposal_index": index,
        "evidence_step_ids": list(map(str, evidence_ids)),
        "draft_saved": True,
        "submitted": False,
    }
    step = db.scalar(
        select(AgentStep).where(
            AgentStep.tenant_id == run.tenant_id,
            AgentStep.run_id == run.id,
            AgentStep.kind == "draft",
            AgentStep.tool_name == "draft_customer_email",
            AgentStep.output["proposal_index"].as_integer() == index,
        )
    )
    if step is not None:
        if (
            step.input != arguments
            or step.output != output
            or step.invoice_id != draft.invoice_id
            or step.status != "completed"
        ):
            raise HTTPException(409, "ACTION_REPLAY_CONFLICT")
        return DraftResult(step_id=step.id)
    step = start_step(
        db,
        run,
        kind="draft",
        tool_name="draft_customer_email",
        invoice_id=draft.invoice_id,
        arguments=arguments,
    )
    finish_step(db, run, step, output=output)
    step.description = "Email draft saved without dispatch authority"
    append_event(db, run, "email.drafted", step.description, step.id)
    db.add(
        AuditEvent(
            tenant_id=run.tenant_id,
            actor_id=run.worker_actor_id,
            correlation_id=run.correlation_id,
            action="email.drafted",
            target_id=step.id,
            details={"run_id": str(run.id), **output},
        )
    )
    return DraftResult(step_id=step.id)


def email_evidence(db: Session, run: AgentRun, invoice: InvoiceSnapshot, ids: list[UUID]) -> list[AgentStep]:
    reads = list(
        db.scalars(
            select(AgentStep)
            .where(
                AgentStep.tenant_id == run.tenant_id,
                AgentStep.run_id == run.id,
                AgentStep.invoice_id == invoice.id,
                AgentStep.id.in_(ids),
                AgentStep.kind == "read",
                AgentStep.status == "completed",
            )
            .order_by(AgentStep.id)
        )
    )
    if (
        invoice.id not in selected_invoices(db, run)
        or len(reads) != len(set(ids))
        or not {"get_invoice", "get_verified_payments"}.issubset(
            {canonical_read_name(r.tool_name) for r in reads}
        )
    ):
        raise HTTPException(403, "EMAIL_EVIDENCE_OUTSIDE_RUN")
    for read in reads:
        if read.tool_name == "get_invoice":
            evidence = ReadEvidence.model_validate_json(json.dumps(read.output))
            invoices = [v for v in evidence.records if isinstance(v, InvoiceFact)]
            if len(invoices) != 1 or (
                invoices[0].invoice_id,
                invoices[0].customer_id,
                invoices[0].source.provider_connection_id,
                invoices[0].source.source_revision,
            ) != (invoice.id, invoice.customer_id, invoice.provider_connection_id, invoice.source_revision):
                raise HTTPException(409, "EMAIL_SOURCE_CHANGED")
    return reads


def execute_email(
    db: Session,
    authority: RunAuthority,
    draft: DraftCustomerEmail,
    index: int,
    evidence_ids: list[UUID],
    *,
    request_send: bool,
) -> ExecutedAction | DraftResult:
    run = current_run(db, authority)
    invoice = db.get(InvoiceSnapshot, draft.invoice_id, populate_existing=True)
    if invoice is None or invoice.tenant_id != run.tenant_id or not invoice.verified:
        raise HTTPException(403, "EMAIL_INVOICE_OUTSIDE_RUN")
    email_evidence(db, run, invoice, evidence_ids)
    kind = "request_email_send" if request_send else "draft_customer_email"
    key = f"agent:{run.id}:proposal:{index}"
    old = db.scalar(
        select(ScheduledAction).where(
            ScheduledAction.tenant_id == run.tenant_id, ScheduledAction.operation_key == key
        )
    )
    if old is not None:
        if (
            old.command.get("kind") != kind
            or old.command.get("payload") != draft.model_dump(mode="json")
            or old.command.get("evidence_step_ids") != list(map(str, evidence_ids))
        ):
            raise HTTPException(409, "ACTION_REPLAY_CONFLICT")
        from aco.features.actions.service import ActionService
        from aco.features.review.email_approvals import current_email_approval
        from aco.features.review.modifications import replacement

        row = db.scalar(
            select(ApprovalRequest).where(
                ApprovalRequest.tenant_id == run.tenant_id, ApprovalRequest.action_id == old.id
            )
        )
        seen: set[UUID] = set()
        while row is not None and (successor := replacement(db, row)) is not None:
            if row.id in seen or row.status != "superseded" or old.status != "cancelled":
                raise HTTPException(409, "INVALID_APPROVAL_LINEAGE")
            seen.add(row.id)
            next_action = db.get(ScheduledAction, successor.action_id, populate_existing=True)
            if (
                next_action is None
                or next_action.tenant_id != run.tenant_id
                or next_action.agent_run_id != run.id
            ):
                raise HTTPException(403, "APPROVAL_REPLACEMENT_OUTSIDE_RUN")
            old, row = next_action, successor
        assert old.agent_step_id is not None
        if old.status in {"succeeded", "failed", "cancelled", "unknown_outcome", "dead_letter"}:
            changes = old.result.get("changed_ids", []) if old.result else []
            return ExecutedAction(
                action_id=old.id,
                step_id=old.agent_step_id,
                changed_ids=[UUID(str(v)) for v in changes] if isinstance(changes, list) else [],
                status=old.status,
                approval_id=row.id if row else None,
            )
        if row is None:
            return ExecutedAction(
                action_id=old.id, step_id=old.agent_step_id, changed_ids=[], status="drafted"
            )
        status = "pending"
        if row.status == "denied":
            old.status, old.last_error, status = "cancelled", "APPROVAL_DENIED", "denied"
            wake_email_run(db, old)
        elif row.status == "approved":
            try:
                current_email_approval(db, old)
            except HTTPException as error:
                old.status, old.last_error, status = "failed", str(error.detail), "stale"
                wake_email_run(db, old)
            else:
                if old.status == "awaiting_approval":
                    old.status, old.due_at = "queued", now()
                    ActionService(db)._outbox(old)
                    append_event(
                        db,
                        run,
                        "email.dispatch_queued",
                        "Approved email queued for current guarded worker checks",
                        old.agent_step_id,
                    )
                status = "queued"
        return ExecutedAction(
            action_id=old.id, step_id=old.agent_step_id, changed_ids=[], status=status, approval_id=row.id
        )
    if not request_send:
        return save_inert_draft(db, run, draft, index, evidence_ids)
    policies = list(
        db.scalars(
            select(CollectionPolicy).where(
                CollectionPolicy.tenant_id == run.tenant_id,
                CollectionPolicy.active.is_(True),
                CollectionPolicy.archived_at.is_(None),
            )
        )
    )
    if len(policies) != 1:
        raise HTTPException(409, "EMAIL_POLICY_REVIEW_REQUIRED")
    policy = policies[0]
    version = db.scalar(
        select(CollectionPolicyVersion)
        .where(
            CollectionPolicyVersion.tenant_id == run.tenant_id, CollectionPolicyVersion.policy_id == policy.id
        )
        .order_by(CollectionPolicyVersion.version.desc())
    )
    assert version is not None
    definition = PolicyDefinition.model_validate(version.definition)
    rules = [r for r in definition.rules if r.channel == "email"]
    if definition.autonomy != "draft_and_review" or not rules:
        raise HTTPException(409, "EMAIL_AI_POLICY_DISABLED")
    rule = sorted(rules, key=lambda r: (r.offset_days, str(r.id)))[0]
    tenant = db.get(Tenant, run.tenant_id)
    connection = db.get(ProviderConnection, invoice.provider_connection_id)
    assert tenant is not None and connection is not None
    activation = latest(db, run.tenant_id)
    if activation is None:
        raise HTTPException(409, "EMAIL_EXECUTION_REVIEW_REQUIRED")
    validate_activation(db, tenant, activation)
    live_cost = live_email_cost_scope(db, activation)
    contact = db.scalar(
        select(CustomerContact).where(
            CustomerContact.tenant_id == run.tenant_id, CustomerContact.customer_id == invoice.customer_id
        )
    )
    if contact is None or contact.email is None or contact.email not in activation.destinations:
        raise HTTPException(409, "EMAIL_RECIPIENT_REVIEW_REQUIRED")
    event = db.scalar(
        select(AccountingEvent)
        .where(
            AccountingEvent.tenant_id == run.tenant_id,
            AccountingEvent.invoice_id == invoice.id,
            AccountingEvent.provider_connection_id == invoice.provider_connection_id,
            AccountingEvent.source_revision == invoice.source_revision,
        )
        .order_by(AccountingEvent.created_at.desc())
    )
    if event is None:
        raise HTTPException(409, "EMAIL_SOURCE_EVENT_UNAVAILABLE")
    material = {
        "kind": kind,
        "payload": draft.model_dump(mode="json"),
        "evidence_step_ids": list(map(str, evidence_ids)),
        "rule_id": str(rule.id),
        "destination": contact.email,
        "email_activation_id": str(activation.id),
        "channel": "email",
    }
    step = start_step(
        db,
        run,
        kind="action",
        tool_name="draft_customer_email",
        invoice_id=invoice.id,
        arguments=draft.model_dump(mode="json"),
    )
    if request_send:
        finish_step(db, run, step, output={"draft_saved": True, "submitted": False})
        step = start_step(
            db,
            run,
            kind="action",
            tool_name="request_email_send",
            invoice_id=invoice.id,
            arguments=draft.model_dump(mode="json"),
        )
    action = ScheduledAction(
        tenant_id=run.tenant_id,
        provider_connection_id=invoice.provider_connection_id,
        invoice_id=invoice.id,
        source_revision=invoice.source_revision,
        source_event_id=None,
        policy_version_id=version.id,
        policy_generation=policy.version,
        initiator_id=run.initiator_id,
        membership_version=run.membership_version,
        tenant_version=run.tenant_generation,
        outbound_paused=tenant.outbound_paused,
        automation_enabled=tenant.automation_enabled,
        worker_actor_id=run.worker_actor_id,
        actor_version=run.actor_version,
        connection_version=connection.version,
        correlation_id=run.correlation_id,
        kind="agent_email",
        operation_key=key,
        command=material,
        due_at=now(),
        status="awaiting_approval" if request_send else "proposed",
        agent_run_id=run.id,
        agent_step_id=step.id,
    )
    db.add(action)
    db.flush()
    row = None
    if request_send:
        proposal = AgentEmailApprovalProposal(
            action_id=action.id,
            action_version=action.version,
            run_id=run.id,
            invoice_ids=(invoice.id,),
            evidence_ids=(event.id,),
            evidence_step_ids=evidence_ids,
            subject=draft.subject,
            content=draft.content,
            destination=contact.email,
            channel="email",
            policy_version_id=version.id,
            cost_bound=live_cost.authorization_ceiling if live_cost else "0",
            cost_basis="SHARED_GRANT_AUTHORIZATION_CEILING" if live_cost else "SYNTHETIC_NO_CHARGE",
            live_cost_scope=live_cost,
            currency="AUD" if activation.provider_binding is None else "USD",
            consent_mode="no_capture",
            rule_id=rule.id,
            email_activation_id=activation.id,
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
        db.flush()
    if not request_send:
        finish_step(
            db, run, step, output={"action_id": str(action.id), "draft_saved": True, "submitted": False}
        )
    append_event(
        db,
        run,
        "approval.requested" if row else "email.drafted",
        "Independent review required; email has not been submitted"
        if row
        else "Email draft saved without dispatch",
        step.id,
    )
    db.add(
        AuditEvent(
            tenant_id=run.tenant_id,
            actor_id=run.worker_actor_id,
            correlation_id=run.correlation_id,
            action="email.approval_requested" if row else "email.drafted",
            target_id=action.id,
            details={"run_id": str(run.id), "submitted": False},
        )
    )
    return ExecutedAction(
        action_id=action.id,
        step_id=step.id,
        changed_ids=[],
        status="pending" if row else "drafted",
        approval_id=row.id if row else None,
    )


def queue_finished_email_run(db: Session, run: AgentRun) -> None:
    if run.status != "awaiting_action":
        return
    unfinished = db.scalar(
        select(ScheduledAction.id)
        .where(
            ScheduledAction.tenant_id == run.tenant_id,
            ScheduledAction.agent_run_id == run.id,
            ScheduledAction.kind == "agent_email",
            ScheduledAction.status.in_(["queued", "executing", "awaiting_approval"]),
        )
        .limit(1)
    )
    if unfinished is not None:
        return
    run.status, run.job_id = "queued", None
    run.job_version += 1
    run.version += 1
    run.lease_token, run.lease_until = None, None
    append_event(
        db,
        run,
        "email.result_resume_queued",
        "Canonical email outcome retained; graph will read the persisted result",
    )


def wake_email_run(db: Session, action: ScheduledAction) -> None:
    if action.kind != "agent_email" or action.agent_run_id is None:
        return
    run = db.get(AgentRun, action.agent_run_id, with_for_update=True, populate_existing=True)
    if run is not None and run.tenant_id == action.tenant_id:
        if action.status in {"succeeded", "failed", "cancelled", "unknown_outcome", "dead_letter"}:
            step = db.get(AgentStep, action.agent_step_id)
            if (
                step is not None
                and step.tenant_id == run.tenant_id
                and step.run_id == run.id
                and step.status == "started"
            ):
                finish_step(
                    db,
                    run,
                    step,
                    output=action.result if action.status == "succeeded" else None,
                    error=action.last_error or action.status if action.status != "succeeded" else None,
                )
        queue_finished_email_run(db, run)
