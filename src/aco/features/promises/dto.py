"""Exact commitment scope and human-confirmation contracts."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, field_serializer, field_validator

from aco.core.types import Money, StrictModel
from aco.features.accounting.dto import validate_currency
from aco.features.cases.dto import CommunicationEvidence, WireModel
from aco.features.voice.evidence_dto import VoiceEvidenceView, VoiceOperationalEvidence


class PromiseAllocation(WireModel):
    invoice_id: UUID
    amount: Money

    @field_validator("amount")
    @classmethod
    def positive_money(cls, value: Money) -> Money:
        validate_currency(value)
        if value.minor_units <= 0:
            raise ValueError("Promise amount must be positive")
        return value


class PromiseInstallmentCommand(WireModel):
    due_date: date
    amount: Money
    allocations: list[PromiseAllocation] = Field(default_factory=list, max_length=100)


class RecordPromise(WireModel):
    customer_id: UUID
    invoice_ids: list[UUID] = Field(default_factory=list, max_length=100)
    candidate_invoice_ids: list[UUID] = Field(default_factory=list, max_length=100)
    evidence: CommunicationEvidence | VoiceOperationalEvidence
    amount: Money | None = None
    allocations: list[PromiseAllocation] = Field(default_factory=list, max_length=100)
    due_date: date | None = None
    timezone: str | None = Field(default=None, max_length=100)
    scope: Literal["explicit", "ambiguous"] = "explicit"
    installments: list[PromiseInstallmentCommand] = Field(default_factory=list, max_length=100)
    replaces_promise_id: UUID | None = None
    replacement_expected_version: int | None = Field(
        default=None, ge=1, exclude_if=lambda value: value is None
    )

    @field_validator("amount")
    @classmethod
    def positive_money(cls, value: Money | None) -> Money | None:
        if value is not None:
            validate_currency(value)
            if value.minor_units <= 0:
                raise ValueError("Promise amount must be positive")
        return value


class ConfirmPromise(WireModel):
    promise_id: UUID
    expected_version: int = Field(ge=1)
    confirmation_evidence: CommunicationEvidence | VoiceOperationalEvidence
    reason: str = Field(min_length=1, max_length=2000)
    replacement_expected_version: int | None = Field(default=None, ge=1)
    active_predecessor_expected_version: int | None = Field(default=None, ge=1)
    # Resolved scope/amount/date is a new commitment via supersede, never a silent rewrite.


class CancelPromise(WireModel):
    promise_id: UUID
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)
    evidence: CommunicationEvidence | VoiceOperationalEvidence


class PromiseMoneyJSON(Money):
    """Exact read serialization without changing canonical command money."""

    @field_serializer("minor_units", when_used="json")
    def exact_minor_units(self, value: int) -> str:
        return str(value)


class PromiseAllocationView(StrictModel):
    invoice_id: UUID
    amount: PromiseMoneyJSON


class PromiseInvoiceView(StrictModel):
    invoice_id: UUID
    amount: PromiseMoneyJSON | None
    hold_id: UUID | None


class PromiseInstallmentView(StrictModel):
    id: UUID
    due_date: date
    cutoff_at: datetime
    amount: PromiseMoneyJSON
    allocations: list[PromiseAllocationView]
    status: str


class PromiseHistoryView(StrictModel):
    id: UUID
    event: str
    actor_id: UUID
    details: dict[str, object]
    created_at: datetime


class PromisePredecessorView(StrictModel):
    id: UUID
    version: int
    status: Literal["ACTIVE", "NEEDS_CONFIRMATION"]


class PromiseView(StrictModel):
    id: UUID
    customer_id: UUID
    status: Literal[
        "CAPTURED", "NEEDS_CONFIRMATION", "ACTIVE", "FULFILLED", "MISSED", "SUPERSEDED", "CANCELLED"
    ]
    version: int
    source_communication_id: UUID | None
    voice_source: VoiceEvidenceView | None = None
    source_text: str
    source_offset: int
    source_at: datetime
    source_state: str
    synthetic: bool
    amount: PromiseMoneyJSON | None
    due_date: date | None
    timezone: str | None
    cutoff_at: datetime | None
    confirmed_at: datetime | None
    confirmation_communication_id: UUID | None
    confirmation_voice_evidence_id: UUID | None = None
    needs_confirmation: bool
    confirmation_reasons: list[str]
    invoice_links: list[PromiseInvoiceView]
    candidate_invoice_ids: list[UUID]
    installments: list[PromiseInstallmentView]
    replaces_promise_id: UUID | None
    superseded_by_id: UUID | None
    replacement_lineage: list[PromisePredecessorView] = Field(default_factory=list)
    review_task_id: UUID | None
    created_at: datetime
    updated_at: datetime
    history: list[PromiseHistoryView]
    verification_status: Literal["UNVERIFIED", "VERIFIED", "PENDING_REVIEW"] = "UNVERIFIED"
    scheduling_status: Literal[
        "PENDING_ENGINE", "PENDING_CONFIRMATION", "QUEUED", "RUNNING", "COMPLETED", "REVIEW_REQUIRED"
    ]


class PromisePage(StrictModel):
    items: list[PromiseView]


class PromiseCheckPlan(StrictModel):
    promise_id: UUID
    promise_version: int
    event_id: UUID
    due_at: datetime
    invoice_ids: list[UUID]
    installment_id: UUID | None
    status: Literal["PENDING_ENGINE", "QUEUED", "RUNNING", "COMPLETED", "REVIEW_REQUIRED"] = "PENDING_ENGINE"
    action_ids: list[UUID] = Field(default_factory=list)
