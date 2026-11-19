"""Current-human text-only replacement of immutable review proposals."""

import json
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.core.types import Role
from aco.features.actions.models import ScheduledAction
from aco.features.agent.events import append_event, finish_step, start_step
from aco.features.agent.models import AgentRun, AgentStep
from aco.features.identity.models import Actor, Membership, now
from aco.features.identity.service import ROLE_PERMISSIONS, IdentityService, Permission
from aco.features.review.approval_dto import (
    ApprovalModificationCommand,
    ApprovalView,
    InternalTaskApprovalProposal,
    InternalTaskEdit,
    OutboundContentEdit,
)
from aco.features.review.email_dto import AgentEmailApprovalProposal
from aco.features.review.internal_work import ApprovalProposal
from aco.features.review.models import ApprovalRequest


def replacement(db: Session, row: ApprovalRequest) -> ApprovalRequest | None:
    return db.scalar(
        select(ApprovalRequest)
        .where(ApprovalRequest.tenant_id == row.tenant_id, ApprovalRequest.supersedes_id == row.id)
        .execution_options(populate_existing=True)
    )


def lineage(db: Session, row: ApprovalRequest) -> tuple[list[dict[str, object]], set[UUID], list[str]]:
    """Bind every actual editor's current authority, never just the last writer."""
    from aco.features.review.approvals import digest, stamp

    bindings: list[dict[str, object]] = []
    editors: set[UUID] = set()
    reasons: list[str] = []
    seen: set[UUID] = set()
    child = row
    while child.supersedes_id is not None:
        if child.id in seen:
            raise HTTPException(409, "INVALID_APPROVAL_LINEAGE")
        seen.add(child.id)
        parent = db.get(ApprovalRequest, child.supersedes_id, populate_existing=True)
        if parent is None or parent.tenant_id != row.tenant_id or child.editor_id is None:
            raise HTTPException(409, "INVALID_APPROVAL_LINEAGE")
        editable = (
            {"action_id", "action_version", "title", "notes"}
            if child.proposal.get("effect") == "internal_task"
            else {"action_id", "action_version", "content"}
        )
        if (
            {k: v for k, v in child.proposal.items() if k not in editable}
            != {k: v for k, v in parent.proposal.items() if k not in editable}
            or child.action_version != parent.action_version + 1
            or child.action_id == parent.action_id
            or child.proposer_id != parent.proposer_id
            or child.initiator_id != parent.initiator_id
            or digest(parent.proposal) != parent.material_hash
            or parent.status != "superseded"
        ):
            reasons.append("APPROVAL_MODIFICATION_SCOPE_CHANGED")
        actor = db.get(Actor, child.editor_id, populate_existing=True)
        member = db.scalar(
            select(Membership)
            .where(Membership.tenant_id == row.tenant_id, Membership.user_id == child.editor_id)
            .execution_options(populate_existing=True)
        )
        if (
            actor is None
            or not actor.active
            or member is None
            or not member.active
            or Permission.APPROVE_EXTERNAL not in ROLE_PERMISSIONS[Role(member.role)]
        ):
            reasons.append("APPROVAL_EDITOR_PERMISSION_CHANGED")
        editors.add(child.editor_id)
        bindings.append(
            {
                "request_id": str(child.id),
                "supersedes_id": str(parent.id),
                "editor": stamp(actor, "kind", "active"),
                "membership": stamp(member, "user_id", "role", "active"),
                "reason": child.edit_reason,
                "parent_material_hash": parent.material_hash,
            }
        )
        child = parent
    return bindings, editors, reasons


def modification_reasons(
    db: Session, row: ApprovalRequest, action: ScheduledAction, scope_reasons: list[str], authorized: bool
) -> list[str]:
    reasons = list(scope_reasons)
    if not authorized:
        reasons.append("APPROVER_PERMISSION_REQUIRED")
    if row.proposal.get("effect") == "voice_session":
        reasons.append("VOICE_MODIFICATION_UNAVAILABLE")
    if row.status not in {"pending", "approved"} or row.consumed_at is not None or replacement(db, row):
        reasons.append("APPROVAL_NOT_MODIFIABLE")
    if action.status not in {"queued", "proposed", "validated", "awaiting_approval"} or action.attempts != 0:
        reasons.append("ACTION_NOT_MODIFIABLE")
    if action.agent_run_id:
        run = db.get(AgentRun, action.agent_run_id, populate_existing=True)
        if run is None or run.status not in {"queued", "awaiting_approval"}:
            reasons.append("RUN_NOT_MODIFIABLE")
    return sorted(set(reasons))


def modify_approval(
    db: Session, ctx: TenantContext, request_id: UUID, command: ApprovalModificationCommand
) -> ApprovalView:
    from aco.features.review.approvals import HumanApprovalService, digest
    from aco.features.tools.gateway import CreateTaskCommand

    service = HumanApprovalService(db)
    service._authorize(ctx, Permission.APPROVE_EXTERNAL)
    row = service._request(ctx, request_id)
    view = service._view(ctx, row)
    if row.version != command.expected_version or row.status not in {"pending", "approved"}:
        raise HTTPException(409, "STALE_APPROVAL")
    if (
        view.action_version != command.reviewed_action_version
        or view.material_hash != command.reviewed_material_hash
        or view.current_binding_hash != command.reviewed_binding_hash
    ):
        raise HTTPException(409, "APPROVAL_REVIEW_CHANGED")
    if not view.can_modify:
        raise HTTPException(409, "APPROVAL_NOT_MODIFIABLE")
    if not command.reason.strip():
        raise HTTPException(422, "APPROVAL_REASON_REQUIRED")
    proposal, old, _, _, _ = service._scope(row)
    changes: dict[str, object]
    if isinstance(proposal, InternalTaskApprovalProposal) and isinstance(command.edit, InternalTaskEdit):
        if not command.edit.title.strip():
            raise HTTPException(422, "APPROVAL_CONTENT_REQUIRED")
        changes = {"title": command.edit.title.strip(), "notes": command.edit.notes}
    elif isinstance(proposal, ApprovalProposal) and isinstance(command.edit, OutboundContentEdit):
        if not command.edit.content.strip():
            raise HTTPException(422, "APPROVAL_CONTENT_REQUIRED")
        changes = {"content": command.edit.content}
    else:
        raise HTTPException(422, "APPROVAL_EDIT_KIND_MISMATCH")
    if all(row.proposal[k] == v for k, v in changes.items()):
        raise HTTPException(422, "APPROVAL_EDIT_UNCHANGED")
    # Copy the immutable exact authority, never promote the human editor to a worker.
    excluded = {
        "id",
        "version",
        "created_at",
        "updated_at",
        "operation_key",
        "command",
        "status",
        "attempts",
        "result",
        "last_error",
        "lease_token",
        "lease_until",
        "job_id",
        "agent_step_id",
    }
    values = {
        column.name: getattr(old, column.name)
        for column in ScheduledAction.__table__.columns
        if column.name not in excluded
    }
    action = ScheduledAction(
        **values,
        id=uuid4(),
        version=old.version + 1,
        operation_key=f"approval-modification:{row.id}",
        command=old.command,
        status="awaiting_approval",
        attempts=0,
        agent_step_id=old.agent_step_id,
    )
    material = {**row.proposal, **changes, "action_id": str(action.id), "action_version": action.version}
    if isinstance(proposal, InternalTaskApprovalProposal):
        parsed = InternalTaskApprovalProposal.model_validate_json(json.dumps(material))
        task = CreateTaskCommand(
            case_binding=parsed.case_binding,
            invoice_id=parsed.invoice_ids[0],
            title=parsed.title,
            notes=parsed.notes,
            evidence_step_ids=parsed.evidence_step_ids,
        )
        action.command = {"kind": "create_collection_task", "payload": task.model_dump(mode="json")}
        run = db.get(AgentRun, old.agent_run_id, populate_existing=True)
        assert run is not None
        prior_step = db.get(AgentStep, old.agent_step_id)
        assert prior_step is not None
        finish_step(db, run, prior_step, error="APPROVAL_SUPERSEDED")
        step = start_step(
            db,
            run,
            kind="action",
            tool_name="create_collection_task",
            invoice_id=old.invoice_id,
            arguments=task.model_dump(mode="json"),
        )
        action.agent_step_id = step.id
        run.status, run.job_id = "awaiting_approval", None
        run.job_version += 1
        run.version += 1
        run.lease_token, run.lease_until = None, None
        append_event(
            db, run, "approval.modified", "Human text edit requires fresh independent approval", step.id
        )
    if isinstance(proposal, AgentEmailApprovalProposal):
        from aco.features.tools.email_dto import DraftCustomerEmail

        parsed_email = AgentEmailApprovalProposal.model_validate_json(json.dumps(material))
        draft = DraftCustomerEmail(
            invoice_id=old.invoice_id, subject=parsed_email.subject, content=parsed_email.content
        )
        action.command = {**old.command, "payload": draft.model_dump(mode="json")}
        run = db.get(AgentRun, old.agent_run_id, populate_existing=True)
        assert run is not None
        step = start_step(
            db,
            run,
            kind="action",
            tool_name="request_email_send",
            invoice_id=old.invoice_id,
            arguments=draft.model_dump(mode="json"),
        )
        finish_step(
            db, run, step, output={"draft_saved": True, "submitted": False, "supersedes_id": str(row.id)}
        )
        action.agent_step_id = step.id
        run.status, run.job_id = "awaiting_approval", None
        run.job_version += 1
        run.version += 1
        run.lease_token, run.lease_until = None, None
        append_event(
            db, run, "approval.modified", "Edited email requires fresh independent approval", step.id
        )
    db.add(action)
    db.flush()
    new = ApprovalRequest(
        tenant_id=row.tenant_id,
        action_id=action.id,
        action_version=action.version,
        proposer_id=row.proposer_id,
        initiator_id=row.initiator_id,
        proposal=material,
        material_hash=digest(material),
        supersedes_id=row.id,
        editor_id=ctx.actor_id,
        edit_reason=command.reason.strip(),
        status="pending",
    )
    db.add(new)
    row.status, row.updated_at = "superseded", now()
    row.version += 1
    old.status = "cancelled"
    old.last_error = "APPROVAL_SUPERSEDED"
    db.flush()
    if isinstance(proposal, InternalTaskApprovalProposal) and proposal.case_binding is not None:
        from aco.features.cases.models import CaseHistory

        db.add(
            CaseHistory(
                tenant_id=ctx.tenant_id,
                case_id=proposal.case_binding.case_id,
                actor_id=ctx.actor_id,
                event="case.approval_modified",
                substantive_human=True,
                details={
                    "approval_id": str(new.id),
                    "supersedes_id": str(row.id),
                    "case_version": proposal.case_binding.case_version,
                    "material_hash": new.material_hash,
                },
            )
        )
        db.flush()
    IdentityService(db).audit(
        ctx,
        "approval.modified",
        new.id,
        {
            "supersedes_id": str(row.id),
            "old_action_id": str(old.id),
            "action_id": str(action.id),
            "material_hash": new.material_hash,
            "reason": command.reason.strip(),
            "dispatch_authorized": False,
        },
    )
    return service._view(ctx, new)
