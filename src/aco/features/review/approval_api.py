"""Current-Membership approval reads and CSRF-protected human decisions."""

from uuid import UUID

from fastapi import APIRouter

from aco.features.identity.api import Database, Subject
from aco.features.identity.service import IdentityService
from aco.features.review.approval_dto import (
    ApprovalDecisionCommand,
    ApprovalModificationCommand,
    ApprovalPage,
    ApprovalView,
)
from aco.features.review.approvals import HumanApprovalService

router = APIRouter(prefix="/api/workspaces/{workspace_id}")


@router.get("/approvals", response_model=ApprovalPage)
async def list_approvals(
    workspace_id: UUID,
    subject: Subject,
    db: Database,
    invoice_id: UUID | None = None,
    action_id: UUID | None = None,
) -> ApprovalPage:
    return await db.run_sync(
        lambda store: HumanApprovalService(store).list(
            IdentityService(store).resolve_context(subject, workspace_id), invoice_id, action_id
        )
    )


@router.get("/approvals/{approval_id}", response_model=ApprovalView)
async def get_approval(workspace_id: UUID, approval_id: UUID, subject: Subject, db: Database) -> ApprovalView:
    return await db.run_sync(
        lambda store: HumanApprovalService(store).get(
            IdentityService(store).resolve_context(subject, workspace_id), approval_id
        )
    )


@router.post("/approvals/{approval_id}/decisions", response_model=ApprovalView)
async def decide_approval(
    workspace_id: UUID, approval_id: UUID, command: ApprovalDecisionCommand, subject: Subject, db: Database
) -> ApprovalView:
    return await db.run_sync(
        lambda store: HumanApprovalService(store).decide_approval(
            IdentityService(store).resolve_context(subject, workspace_id), approval_id, command
        )
    )


@router.post("/approvals/{approval_id}/modifications", response_model=ApprovalView)
async def modify_approval(
    workspace_id: UUID,
    approval_id: UUID,
    command: ApprovalModificationCommand,
    subject: Subject,
    db: Database,
) -> ApprovalView:
    from aco.features.review.modifications import modify_approval

    return await db.run_sync(
        lambda store: modify_approval(
            store, IdentityService(store).resolve_context(subject, workspace_id), approval_id, command
        )
    )
