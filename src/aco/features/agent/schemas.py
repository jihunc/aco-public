"""Bounded investigation API. Actual action outcomes are assigned by the domain gateway."""

from typing import Annotated, Literal, TypedDict
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aco.core.types import StrictModel
from aco.features.cases.dto import UpsertCase
from aco.features.cases.task_binding import TaskCaseBinding
from aco.features.communications.association_dto import LinkCommunication
from aco.features.promises.dto import RecordPromise
from aco.features.tools.contact_stop import RequestCollectionHold
from aco.features.tools.email_dto import DraftCustomerEmail
from aco.features.tools.task_update import TaskUpdate
from aco.features.voice.agent_request import RequestVoiceCall

RunStatus = Literal[
    "queued",
    "running",
    "awaiting_approval",
    "awaiting_action",
    "completed",
    "partially_completed",
    "failed",
    "cancelled",
]


class AgentTrigger(StrictModel):
    invoice_ids: Annotated[list[Annotated[UUID, Field(strict=False)]], Field(min_length=1, max_length=3)]
    objective: Annotated[str, Field(min_length=1, max_length=1000)]
    request_id: Annotated[UUID, Field(strict=False)]

    @field_validator("invoice_ids")
    @classmethod
    def unique(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("Duplicate invoice selection")
        return value


class CancelRun(StrictModel):
    expected_version: Annotated[int, Field(ge=1)]


class InvoiceFinding(StrictModel):
    invoice_id: UUID
    assessment: Annotated[str, Field(min_length=1, max_length=1000)]
    evidence_step_ids: Annotated[list[UUID], Field(min_length=2, max_length=8)]


class Proposal(StrictModel):
    invoice_id: UUID
    kind: Literal[
        "draft_customer_email",
        "request_email_send",
        "human_review",
        "request_human_review",
        "schedule_followup",
        "verify_accounting",
        "follow_up",
        "create_task",
        "update_collection_task",
        "upsert_case",
        "record_promise",
        "request_task_approval",
        "request_collection_hold",
        "request_voice_call",
        "link_communication",
    ]
    reason: Annotated[str, Field(min_length=1, max_length=600)]
    executed: bool = False
    title: str | None = Field(default=None, max_length=240)
    task_update: TaskUpdate | None = None
    followup_at: AwareDatetime | None = None
    case_binding: TaskCaseBinding | None = None
    case_command: UpsertCase | None = None
    promise_command: RecordPromise | None = None
    hold_command: RequestCollectionHold | None = None
    voice_command: RequestVoiceCall | None = None
    link_command: LinkCommunication | None = None
    voice_session_id: UUID | None = None
    email_command: DraftCustomerEmail | None = None
    approval_id: UUID | None = None
    approval_status: Literal["pending", "executed", "denied", "stale"] | None = None
    action_id: UUID | None = None
    changed_ids: list[UUID] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def exact_command(self) -> "Proposal":
        if (self.kind == "update_collection_task") != (self.task_update is not None):
            raise ValueError("Task update must match proposal kind")
        if (self.kind == "link_communication") != (self.link_command is not None):
            raise ValueError("Communication link command must match proposal kind")
        if self.link_command is not None and self.invoice_id not in self.link_command.invoice_ids:
            raise ValueError("Communication link primary invoice must be in scope")
        if (self.kind == "request_voice_call") != (self.voice_command is not None):
            raise ValueError("Voice command must match proposal kind")
        if self.voice_command is not None and self.voice_command.invoice_id != self.invoice_id:
            raise ValueError("Voice command invoice must match proposal")
        if (self.kind in {"draft_customer_email", "request_email_send"}) != (self.email_command is not None):
            raise ValueError("Email command must match proposal kind")
        if self.email_command is not None and self.email_command.invoice_id != self.invoice_id:
            raise ValueError("Email command invoice must match proposal")
        if self.kind == "schedule_followup" and self.followup_at is None:
            raise ValueError("Scheduled human follow-up requires an explicit due time")
        if self.followup_at is not None and self.kind not in {"schedule_followup", "follow_up"}:
            raise ValueError("Follow-up time requires a human follow-up proposal")
        if self.case_binding is not None and self.kind not in {
            "create_task",
            "request_task_approval",
            "request_human_review",
            "schedule_followup",
            "human_review",
            "follow_up",
            "verify_accounting",
        }:
            raise ValueError("Case binding requires an internal task")
        if (self.kind == "upsert_case") != (self.case_command is not None):
            raise ValueError("Case command must match proposal kind")
        if (self.kind == "record_promise") != (self.promise_command is not None):
            raise ValueError("Promise command must match proposal kind")
        if (self.kind == "request_collection_hold") != (self.hold_command is not None):
            raise ValueError("Collection hold command must match proposal kind")
        return self


class InvestigationResult(StrictModel):
    summary: Annotated[str, Field(min_length=1, max_length=1000)]
    invoice_findings: Annotated[list[InvoiceFinding], Field(min_length=1, max_length=3)]
    proposals: Annotated[list[Proposal], Field(max_length=6)]
    limitations: Annotated[list[str], Field(max_length=8)]


class RunView(StrictModel):
    id: UUID
    source_receipt_id: UUID | None = None
    source_communication_id: UUID | None = None
    status: RunStatus
    version: int
    invoice_ids: list[UUID]
    objective: str
    authenticity: Literal["FAKE", "LIVE"]
    model_id: str | None
    model_route: str
    result: InvestigationResult | None
    error_code: str | None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    last_event_sequence: int
    execution: Literal["UNAVAILABLE", "PROPOSED", "EXECUTED", "PARTIAL"] = "UNAVAILABLE"
    usage_ids: list[UUID]
    input_tokens: int | None
    output_tokens: int | None
    requests: int
    provider_attempted: bool


class RunPage(StrictModel):
    items: list[RunView]


class StepView(StrictModel):
    id: UUID
    run_id: UUID
    step_sequence: int
    kind: Literal["model", "read", "action"]
    status: Literal["started", "completed", "failed"]
    tool_name: str | None
    description: str
    invoice_id: UUID | None
    input: dict[str, object]
    output: dict[str, object] | None
    error_code: str | None
    usage_ids: list[UUID]
    created_at: AwareDatetime
    updated_at: AwareDatetime


class AgentHistory(StrictModel):
    items: list[StepView]
    last_event_sequence: int


class EventView(StrictModel):
    id: str
    run_id: UUID
    event_sequence: int
    kind: str
    step_id: UUID | None
    description: str
    created_at: AwareDatetime


class EventPage(StrictModel):
    items: list[EventView]
    last_event_sequence: int


class GraphState(TypedDict, total=False):
    run_id: str
    analysis_saved: bool
    awaiting_action: bool
    awaiting_approval: bool
    actions_saved: bool
    completed: bool
