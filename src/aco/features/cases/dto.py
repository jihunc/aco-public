"""Evidence-bearing case commands. No caller-supplied tenant or authority flags."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, ValidationInfo, field_validator

from aco.core.types import StrictModel
from aco.features.voice.evidence_dto import VoiceEvidenceView, VoiceOperationalEvidence

CaseStatus = Literal[
    "OPEN", "INVESTIGATING", "WAITING_CUSTOMER", "WAITING_INTERNAL", "AWAITING_APPROVAL", "RESOLVED", "CLOSED"
]
IssueType = Literal[
    "wrong_amount", "wrong_po", "payment_not_found", "refund", "contact_correction", "stop_contact", "other"
]


class WireModel(StrictModel):
    @field_validator("*", mode="before")
    @classmethod
    def wire_values(cls, value: object, info: ValidationInfo) -> object:
        assert info.field_name is not None
        if isinstance(value, str):
            if info.field_name.endswith("_id"):
                return UUID(value)
            if info.field_name.endswith("_at"):
                return datetime.fromisoformat(value)
            if info.field_name == "due_date":
                return date.fromisoformat(value)
        if info.field_name in {"invoice_ids", "candidate_invoice_ids"} and isinstance(value, list):
            return [UUID(v) if isinstance(v, str) else v for v in value]
        return value


class CommunicationEvidence(WireModel):
    communication_id: UUID
    excerpt: str = Field(min_length=1, max_length=8000)
    offset: int = Field(ge=0)


class UpsertCase(WireModel):
    customer_id: UUID
    issue_type: IssueType
    claim_key: str = Field(min_length=1, max_length=240)
    substance: str = Field(min_length=1, max_length=2000)
    invoice_ids: list[UUID] = Field(min_length=1, max_length=100)
    evidence: CommunicationEvidence | VoiceOperationalEvidence
    matching: Literal["new", "same", "ambiguous"] = "new"
    matched_case_id: UUID | None = None
    expected_version: int | None = Field(default=None, ge=1)


class LinkCaseEvidence(WireModel):
    case_id: UUID
    evidence: CommunicationEvidence | VoiceOperationalEvidence
    expected_version: int = Field(ge=1)


class TransitionCase(WireModel):
    case_id: UUID
    status: CaseStatus
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)
    evidence: CommunicationEvidence | VoiceOperationalEvidence | None = None
    closure_reason: (
        Literal["resolved", "duplicate_linked", "withdrawn", "out_of_scope", "invalid_claim"] | None
    ) = None
    surviving_case_id: UUID | None = None


class InvoiceIssueTransition(WireModel):
    case_id: UUID
    invoice_id: UUID
    expected_version: int = Field(ge=1)
    evidence: CommunicationEvidence | VoiceOperationalEvidence
    resolution_kind: Literal[
        "documentary_confirmation",
        "payment_allocation",
        "source_refund",
        "verified_contact_restriction",
        "verified_contact_correction",
    ]
    resolution_reference_id: UUID | None = None
    expected_reference_version: int | None = Field(default=None, ge=1)
    expected_reference_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    resolution_action: str = Field(min_length=1, max_length=2000)


class DispositionReference(StrictModel):
    id: UUID
    version: int
    resolution_kind: Literal["source_refund", "verified_contact_restriction", "verified_contact_correction"]
    source_type: Literal[
        "verified_refund", "enforced_contact_stop", "verified_contact_review", "verified_opt_out"
    ]
    label: str
    material_hash: str
    synthetic: bool
    details: dict[str, object]


class DispositionReferences(StrictModel):
    case_id: UUID
    case_version: int
    invoice_id: UUID
    items: list[DispositionReference]
    unavailable_reason: str | None = None


class EvidenceView(StrictModel):
    id: UUID
    communication_id: UUID | None
    voice_source: VoiceEvidenceView | None = None
    excerpt: str
    offset: int
    kind: str
    synthetic: bool
    source_state: str
    created_at: datetime


class CaseInvoiceView(StrictModel):
    invoice_id: UUID
    issue_state: str
    impact: str
    hold_id: UUID | None
    resolution_action: str | None
    resolution_evidence_id: UUID | None
    resolved_at: datetime | None


class CaseHistoryView(StrictModel):
    id: UUID
    event: str
    actor_id: UUID
    substantive_human: bool
    details: dict[str, object]
    created_at: datetime


class CaseView(StrictModel):
    id: UUID
    customer_id: UUID
    issue_type: str
    claim_key: str
    substance: str
    status: CaseStatus
    version: int
    opened_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
    closed_at: datetime | None
    closure_reason: str | None
    review_required: bool
    task_id: UUID
    synthetic: bool
    invoice_links: list[CaseInvoiceView]
    evidence: list[EvidenceView]
    history: list[CaseHistoryView]


class CaseMutation(StrictModel):
    case: CaseView
    created: bool
    review_required: bool


class CasePage(StrictModel):
    items: list[CaseView]
