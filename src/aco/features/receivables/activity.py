"""Finite invoice-linked audit metadata. Never expose the general audit payload ledger."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import Field
from sqlalchemy import Select, and_, literal, or_, select, union_all
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from aco.core.context import ActorContext, TenantContext
from aco.core.types import StrictModel
from aco.features.accounting.models import InvoiceSnapshot
from aco.features.actions.models import ScheduledAction
from aco.features.cases.models import CaseInvoiceLink
from aco.features.communications.models import CommunicationInvoiceLink
from aco.features.identity.models import Actor, AuditEvent
from aco.features.identity.service import IdentityService, Permission
from aco.features.policies.models import CollectionHold
from aco.features.promises.models import PromiseInvoiceLink
from aco.features.review.models import ApprovalRequest, CollectionTask

TargetKind = Literal["invoice", "task", "case", "promise", "action", "hold", "communication", "approval"]


class InvoiceAuditQuery(StrictModel):
    limit: int = Field(default=50, ge=1, le=100, strict=False)
    before_id: UUID | None = Field(default=None, strict=False)


class InvoiceAuditItem(StrictModel):
    id: UUID
    created_at: datetime
    action: str
    actor_id: UUID
    actor_kind: Literal["human", "service"]
    target_kind: TargetKind
    target_id: UUID
    target_available: bool
    correlation_id: UUID


class InvoiceAuditPage(StrictModel):
    invoice_id: UUID
    items: list[InvoiceAuditItem]
    next_before_id: UUID | None


def invoice_audit(
    db: Session, ctx: ActorContext, invoice_id: UUID, query: InvoiceAuditQuery
) -> InvoiceAuditPage:
    if not isinstance(ctx, TenantContext):
        raise HTTPException(403, "HUMAN_REQUIRED")
    IdentityService(db).authorize(ctx, Permission.INVOICE_READ)
    invoice = db.scalar(
        select(InvoiceSnapshot.id).where(
            InvoiceSnapshot.tenant_id == ctx.tenant_id, InvoiceSnapshot.id == invoice_id
        )
    )
    if invoice is None:
        raise HTTPException(404, "INVOICE_NOT_FOUND")

    # Each branch proves both the canonical invoice relationship and the action's
    # target type. UUID coincidence and arbitrary JSON audit details are not scope.
    action_ids = select(ScheduledAction.id).where(
        ScheduledAction.tenant_id == ctx.tenant_id, ScheduledAction.invoice_id == invoice_id
    )
    task_ids = select(CollectionTask.id).where(
        CollectionTask.tenant_id == ctx.tenant_id, CollectionTask.invoice_id == invoice_id
    )
    deleted_tasks = select(CollectionTask.id).where(
        CollectionTask.tenant_id == ctx.tenant_id, CollectionTask.deleted_at.is_not(None)
    )

    def branch(
        kind: TargetKind,
        predicate: ColumnElement[bool],
        names: tuple[str, ...],
        available: ColumnElement[bool] | None = None,
    ) -> Select[UUID, TargetKind, bool]:
        return select(
            AuditEvent.id.label("event_id"),
            literal(kind).label("target_kind"),
            (available if available is not None else literal(True)).label("target_available"),
        ).where(AuditEvent.tenant_id == ctx.tenant_id, predicate, AuditEvent.action.in_(names))

    scope = union_all(
        branch("invoice", AuditEvent.target_id == invoice_id, ("accounting.reconciled",)),
        branch(
            "task",
            AuditEvent.target_id.in_(task_ids),
            (
                "task.created",
                "task.updated",
                "task.deleted",
                "task.created_from_business_record",
                "task.created_by_agent",
                "task.updated_by_agent",
                "task.applied",
                "task.created_by_voice",
                "agent.request_human_review",
                "agent.schedule_followup",
            ),
            AuditEvent.target_id.not_in(deleted_tasks),
        ),
        branch(
            "case",
            AuditEvent.target_id.in_(
                select(CaseInvoiceLink.case_id).where(
                    CaseInvoiceLink.tenant_id == ctx.tenant_id, CaseInvoiceLink.invoice_id == invoice_id
                )
            ),
            (
                "case.created",
                "case.evidence_added",
                "case.invoice_issue_resolved",
                "case.reopened",
                "case.transitioned",
                "case.approval_modified",
            ),
        ),
        branch(
            "promise",
            AuditEvent.target_id.in_(
                select(PromiseInvoiceLink.promise_id).where(
                    PromiseInvoiceLink.tenant_id == ctx.tenant_id, PromiseInvoiceLink.invoice_id == invoice_id
                )
            ),
            (
                "promise.captured",
                "promise.confirmed",
                "promise.cancelled",
                "promise.superseded",
                "promise.performance_status",
                "promise.verification_pending",
                "promise.performance_verified",
                "promise.installment_status",
            ),
        ),
        branch(
            "action",
            AuditEvent.target_id.in_(action_ids),
            (
                "action.queued",
                "action.claimed",
                "action.cancelled",
                "action.succeeded",
                "action.failed",
                "action.dead_letter",
                "action.unknown_outcome",
                "email.run_due_requested",
                "promise.check_queued",
            ),
        ),
        branch(
            "hold",
            AuditEvent.target_id.in_(
                select(CollectionHold.id).where(
                    CollectionHold.tenant_id == ctx.tenant_id, CollectionHold.invoice_id == invoice_id
                )
            ),
            ("collection.hold_placed", "collection.hold_released"),
        ),
        branch(
            "communication",
            AuditEvent.target_id.in_(
                select(CommunicationInvoiceLink.communication_id).where(
                    CommunicationInvoiceLink.tenant_id == ctx.tenant_id,
                    CommunicationInvoiceLink.invoice_id == invoice_id,
                )
            ),
            ("email.prepared", "email.delivery_reconciled", "communication.invoices_linked"),
        ),
        branch(
            "approval",
            AuditEvent.target_id.in_(
                select(ApprovalRequest.id).where(
                    ApprovalRequest.tenant_id == ctx.tenant_id, ApprovalRequest.action_id.in_(action_ids)
                )
            ),
            (
                "approval.requested",
                "approval.consumed",
                "approval.resume_queued",
                "approval.modified",
                "approval.approved",
                "approval.denied",
                "approval.consumed_for_email",
            ),
        ),
    ).subquery()
    statement = (
        select(
            AuditEvent.id,
            AuditEvent.created_at,
            AuditEvent.action,
            AuditEvent.actor_id,
            Actor.kind.label("actor_kind"),
            scope.c.target_kind,
            AuditEvent.target_id,
            scope.c.target_available,
            AuditEvent.correlation_id,
        )
        .join(scope, scope.c.event_id == AuditEvent.id)
        .join(Actor, Actor.id == AuditEvent.actor_id)
        .where(
            AuditEvent.tenant_id == ctx.tenant_id,
            or_(
                and_(Actor.kind == "human", Actor.tenant_id.is_(None)),
                and_(Actor.kind == "service", Actor.tenant_id == ctx.tenant_id),
            ),
        )
    )
    if query.before_id is not None:
        cursor = db.execute(statement.where(AuditEvent.id == query.before_id)).mappings().one_or_none()
        if cursor is None:
            raise HTTPException(404, "AUDIT_CURSOR_NOT_FOUND")
        statement = statement.where(
            or_(
                AuditEvent.created_at < cursor["created_at"],
                and_(AuditEvent.created_at == cursor["created_at"], AuditEvent.id < query.before_id),
            )
        )
    rows = (
        db.execute(
            statement.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(query.limit + 1)
        )
        .mappings()
        .all()
    )
    items = [InvoiceAuditItem.model_validate(dict(row)) for row in rows[: query.limit]]
    return InvoiceAuditPage(
        invoice_id=invoice_id, items=items, next_before_id=items[-1].id if len(rows) > query.limit else None
    )
