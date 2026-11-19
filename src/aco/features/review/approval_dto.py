"""Human review of exact proposals; these DTOs never authorize dispatch."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from aco.core.types import StrictModel
from aco.features.cases.task_binding import TaskCaseBinding
from aco.features.receivables.queries import MoneyJSON
from aco.features.review.email_dto import AgentEmailApprovalProposal
from aco.features.review.internal_work import ApprovalProposal
from aco.features.voice.dto import VoiceApprovalProposal


class InternalTaskApprovalProposal(StrictModel):
    case_binding: TaskCaseBinding | None = Field(default=None, exclude_if=lambda value: value is None)
    effect: Literal["internal_task"] = "internal_task"
    action_id: UUID
    action_version: int = Field(ge=1)
    run_id: UUID
    invoice_ids: list[UUID] = Field(min_length=1, max_length=1)
    evidence_step_ids: list[UUID] = Field(min_length=2, max_length=8)
    title: str = Field(min_length=1, max_length=240)
    notes: str = Field(max_length=4000)


class AgentApprovalEvidence(StrictModel):
    step_id: UUID
    run_id: UUID
    tool_name: str
    source_json: str


class InternalTaskEdit(StrictModel):
    kind: Literal["internal_task"]
    title: str = Field(min_length=1, max_length=240)
    notes: str = Field(max_length=4000)


class OutboundContentEdit(StrictModel):
    kind: Literal["outbound_content"]
    content: str = Field(min_length=1, max_length=20000)


class ApprovalModificationCommand(StrictModel):
    expected_version: int = Field(ge=1)
    reviewed_action_version: int = Field(ge=1)
    reviewed_material_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_binding_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    edit: InternalTaskEdit | OutboundContentEdit = Field(discriminator="kind")
    reason: str = Field(min_length=1, max_length=2000)


class ApprovalDecisionCommand(StrictModel):
    expected_version: int = Field(ge=1)
    reviewed_action_version: int = Field(ge=1)
    reviewed_material_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_binding_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["approved", "denied"]
    reason: str = Field(min_length=1, max_length=2000)
    expires_in_seconds: int = Field(default=86400, ge=1, le=86400)


class ApprovalDecisionView(StrictModel):
    id: UUID
    approver_id: UUID
    decision: str
    reason: str
    material_hash: str
    created_at: datetime


class ApprovalEvidenceView(StrictModel):
    id: UUID
    invoice_id: UUID
    source_revision: str
    origin: str
    source_json: str


class ApprovalInvoiceScope(StrictModel):
    invoice_id: UUID
    number: str
    customer_id: UUID
    source_revision: str
    outstanding: MoneyJSON
    financial_status: str
    observed_at: datetime
    synthetic: bool
    verified: bool


class ApprovalView(StrictModel):
    can_modify: bool = False
    modification_reasons: list[str] = Field(default_factory=list)
    supersedes_id: UUID | None = None
    replacement_id: UUID | None = None
    editor_id: UUID | None = None
    edit_reason: str | None = None
    id: UUID
    version: int
    action_id: UUID
    action_version: int
    proposer_id: UUID
    initiator_id: UUID
    source_action_initiator_id: UUID
    customer_id: UUID
    material_hash: str
    current_binding_hash: str
    proposal: (
        AgentEmailApprovalProposal | ApprovalProposal | InternalTaskApprovalProposal | VoiceApprovalProposal
    )
    invoice_scope: ApprovalInvoiceScope
    evidence: list[ApprovalEvidenceView]
    status: str
    effective_status: Literal["pending", "approved", "denied", "expired", "stale", "consumed"]
    approval_valid: bool
    new_approval_required: bool
    can_approve: bool
    can_deny: bool
    reasons: list[str]
    granted_at: datetime | None
    expires_at: datetime | None
    consumed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    decisions: list[ApprovalDecisionView]
    effect: Literal["outbound_message", "internal_task", "voice_session"] = "outbound_message"
    source_run_id: UUID | None = None
    source_step_id: UUID | None = None
    agent_evidence: list[AgentApprovalEvidence] = Field(default_factory=list)
    changed_ids: list[UUID] = Field(default_factory=list)
    execution_status: Literal["pending_approval", "queued", "executed", "denied", "stale", "failed"] = (
        "pending_approval"
    )
    dispatch_status: Literal[
        "NOT_ENABLED", "NOT_APPLICABLE", "AWAITING_REVIEW", "QUEUED", "ACCEPTED", "UNKNOWN", "STOPPED"
    ] = "NOT_ENABLED"


class ApprovalPage(StrictModel):
    items: list[ApprovalView]
