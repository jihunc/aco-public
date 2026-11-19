"""Guarded internal records only. Human approval/CRUD and queue ordering belong to T16."""

import hashlib
import json
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException
from pydantic import AwareDatetime, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import ActorContext, ServiceContext
from aco.core.types import StrictModel, validate_charge
from aco.features.accounting.models import AccountingEvent, InvoiceSnapshot
from aco.features.actions.models import ScheduledAction
from aco.features.identity.models import Membership
from aco.features.identity.service import IdentityService, Permission
from aco.features.review.models import ApprovalRequest, CollectionTask


class InternalTaskCommand(StrictModel):
    source_action_id: UUID
    source_event_id: UUID
    invoice_id: UUID
    title: str = Field(min_length=1, max_length=240)
    notes: str = Field(default="", max_length=4000)
    assignee_id: UUID | None = None
    due_at: AwareDatetime | None = None
    expected_version: int | None = None


class TaskRef(StrictModel):
    task_id: UUID
    version: int


class ApprovalProposal(StrictModel):
    action_id: UUID
    action_version: int
    content: str = Field(min_length=1, max_length=20000)
    destination: str = Field(min_length=1, max_length=320)
    channel: str = Field(pattern="^(email|voice)$")
    invoice_ids: tuple[UUID, ...] = Field(min_length=1)
    evidence_ids: tuple[UUID, ...] = Field(min_length=1)
    policy_version_id: UUID
    cost_bound: str
    currency: str = Field(pattern="^[A-Z]{3}$")
    consent_mode: str = Field(pattern="^(no_capture|approved_capture)$")

    @field_validator("cost_bound")
    @classmethod
    def exact_cost(cls, value: str) -> str:
        validate_charge(Decimal(value))
        return value


class ApprovalRequestRef(StrictModel):
    request_id: UUID


class InternalWorkService:
    def __init__(self, db: Session):
        self.db = db

    def action(self, ctx: ActorContext, action_id: UUID) -> ScheduledAction:
        IdentityService(self.db).authorize(ctx, Permission.TASK_WRITE)
        action = self.db.scalar(
            select(ScheduledAction).where(
                ScheduledAction.id == action_id, ScheduledAction.tenant_id == ctx.tenant_id
            )
        )
        if action is None:
            raise HTTPException(403, "FOREIGN_ACTION")
        if isinstance(ctx, ServiceContext):
            grant = self.db.get(ScheduledAction, ctx.action_id, populate_existing=True)
            if (
                grant is None
                or grant.invoice_id != action.invoice_id
                or grant.provider_connection_id != action.provider_connection_id
                or grant.source_revision != action.source_revision
                or grant.policy_version_id != action.policy_version_id
            ):
                raise HTTPException(403, "FOREIGN_ACTION")
        return action

    def exact_command(self, ctx: ActorContext, kind: str, command: StrictModel) -> None:
        if isinstance(ctx, ServiceContext):
            grant = self.db.get(ScheduledAction, ctx.action_id, populate_existing=True)
            if (
                grant is None
                or grant.kind != kind
                or grant.command["payload"] != command.model_dump(mode="json")
            ):
                raise HTTPException(403, "COMMAND_OUTSIDE_ACTION_GRANT")

    def apply_internal_task(self, ctx: ActorContext, command: InternalTaskCommand) -> TaskRef:
        self.exact_command(ctx, "internal_task", command)
        action = self.action(ctx, command.source_action_id)
        event = self.db.get(AccountingEvent, command.source_event_id)
        if (
            event is None
            or event.tenant_id != ctx.tenant_id
            or event.invoice_id != command.invoice_id
            or command.invoice_id != action.invoice_id
        ):
            raise HTTPException(403, "FOREIGN_TASK_SCOPE")
        if command.assignee_id is not None:
            member = self.db.scalar(
                select(Membership).where(
                    Membership.tenant_id == ctx.tenant_id,
                    Membership.user_id == command.assignee_id,
                    Membership.active.is_(True),
                )
            )
            if member is None:
                raise HTTPException(403, "FOREIGN_ASSIGNEE")
        material = command.model_dump(mode="json", exclude={"expected_version"})
        row = self.db.scalar(
            select(CollectionTask)
            .where(CollectionTask.tenant_id == ctx.tenant_id, CollectionTask.source_action_id == action.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row:
            if row.command == material:
                return TaskRef(task_id=row.id, version=row.version)
            if command.expected_version != row.version or row.deleted_at:
                raise HTTPException(409, "STALE_TASK")
            row.version += 1
        else:
            if command.expected_version is not None:
                raise HTTPException(409, "TASK_NOT_FOUND")
            row = CollectionTask(
                tenant_id=ctx.tenant_id,
                source_action_id=action.id,
                source_event_id=event.id,
                invoice_id=command.invoice_id,
                creator_id=ctx.actor_id,
            )
            self.db.add(row)
        row.title, row.notes, row.assignee_id, row.due_at = (
            command.title,
            command.notes,
            command.assignee_id,
            command.due_at,
        )
        row.command = material
        self.db.flush()
        IdentityService(self.db).audit(ctx, "task.applied", row.id)
        return TaskRef(task_id=row.id, version=row.version)

    def request_approval(self, ctx: ActorContext, command: ApprovalProposal) -> ApprovalRequestRef:
        self.exact_command(ctx, "approval_request", command)
        action = self.action(ctx, command.action_id)
        if command.action_version != action.version or command.policy_version_id != action.policy_version_id:
            raise HTTPException(409, "STALE_PROPOSAL")
        for invoice_id in command.invoice_ids:
            invoice = self.db.get(InvoiceSnapshot, invoice_id)
            if invoice is None or invoice.tenant_id != ctx.tenant_id or invoice.id != action.invoice_id:
                raise HTTPException(403, "FOREIGN_APPROVAL_SCOPE")
        for evidence_id in command.evidence_ids:
            event = self.db.get(AccountingEvent, evidence_id)
            if (
                event is None
                or event.tenant_id != ctx.tenant_id
                or event.invoice_id not in command.invoice_ids
            ):
                raise HTTPException(403, "FOREIGN_APPROVAL_EVIDENCE")
        material = command.model_dump(mode="json")
        digest = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
        prior = self.db.scalar(
            select(ApprovalRequest).where(
                ApprovalRequest.tenant_id == ctx.tenant_id,
                ApprovalRequest.action_id == action.id,
                ApprovalRequest.action_version == action.version,
                ApprovalRequest.material_hash == digest,
            )
        )
        if prior:
            return ApprovalRequestRef(request_id=prior.id)
        initiator = ctx.actor_id
        if isinstance(ctx, ServiceContext):
            grant = self.db.get(ScheduledAction, ctx.action_id)
            assert grant is not None
            initiator = grant.initiator_id
        request = ApprovalRequest(
            tenant_id=ctx.tenant_id,
            action_id=action.id,
            action_version=action.version,
            proposer_id=ctx.actor_id,
            initiator_id=initiator,
            material_hash=digest,
            proposal=material,
        )
        self.db.add(request)
        self.db.flush()
        IdentityService(self.db).audit(ctx, "approval.requested", request.id)
        return ApprovalRequestRef(request_id=request.id)
