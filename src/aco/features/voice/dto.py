"""Bounded synthetic browser sessions; no destination, secret or capture permission from callers."""

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from aco.core.types import Money, StrictModel
from aco.features.artifacts.consent_dto import CaptureScopes, VoiceConsentView
from aco.features.artifacts.dto import ArtifactRef
from aco.features.cases.dto import IssueType, UpsertCase, WireModel
from aco.features.promises.dto import PromiseAllocation, PromiseInstallmentCommand, RecordPromise
from aco.features.voice.evidence_dto import VoiceEvidenceView
from aco.integrations.ports.voice import VOICE_TOOLS, VoiceTool


class PrepareVoice(StrictModel):
    request_id: Annotated[UUID, Field(strict=False)]
    connection_id: Annotated[UUID, Field(strict=False)]
    invoice_ids: list[Annotated[UUID, Field(strict=False)]] = Field(min_length=1, max_length=3)
    objective: str = Field(min_length=1, max_length=1000)
    demo_scenario: Literal["task", "business_facts"] = Field(
        default="task", exclude_if=lambda value: value == "task"
    )
    consent_id: Annotated[UUID, Field(strict=False)] | None = None


class VoiceTaskCommand(StrictModel):
    invoice_id: UUID
    title: str = Field(min_length=1, max_length=240)


class VoiceCaseCommand(StrictModel):
    invoice_id: UUID
    case_command: UpsertCase


class VoicePromiseCommand(StrictModel):
    invoice_id: UUID
    promise_command: RecordPromise


class VoiceCaseFacts(WireModel):
    invoice_ids: list[UUID] = Field(min_length=1, max_length=3)
    issue_type: IssueType
    claim_reference: str = Field(pattern=r"^[a-z0-9][a-z0-9._:-]{0,79}$")
    matching: Literal["new", "same", "ambiguous"] = "new"
    matched_case_id: UUID | None = None
    expected_version: int | None = Field(default=None, ge=1)


class VoicePromiseFacts(WireModel):
    invoice_ids: list[UUID] = Field(default_factory=list, max_length=3)
    amount: Money | None = None
    due_date: date | None = None
    timezone: str | None = Field(default=None, max_length=100)
    scope: Literal["explicit", "ambiguous"] = "explicit"
    allocations: list[PromiseAllocation] = Field(default_factory=list, max_length=3)
    installments: list[PromiseInstallmentCommand] = Field(default_factory=list, max_length=100)
    candidate_invoice_ids: list[UUID] = Field(default_factory=list, max_length=3)
    replaces_promise_id: UUID | None = None


class VoiceHoldFacts(WireModel):
    invoice_ids: list[UUID] = Field(min_length=1, max_length=1)
    reason_code: Literal["customer_contact_stop"] = "customer_contact_stop"


class VoiceCaseFactCommand(StrictModel):
    invoice_id: UUID
    case_facts: VoiceCaseFacts


class VoicePromiseFactCommand(StrictModel):
    invoice_id: UUID
    promise_facts: VoicePromiseFacts


class VoiceHoldFactCommand(StrictModel):
    invoice_id: UUID
    hold_facts: VoiceHoldFacts


class VoiceApprovalProposal(StrictModel):
    effect: Literal["voice_session"] = "voice_session"
    action_id: UUID
    action_version: int
    session_id: UUID
    invoice_ids: list[UUID]
    objective: str
    demo_scenario: Literal["task", "business_facts"] = Field(
        default="task", exclude_if=lambda value: value == "task"
    )
    allowed_tools: tuple[VoiceTool, ...] = VOICE_TOOLS
    evidence_mode: Literal["MOCKED_HTTP", "LIVE"] = "MOCKED_HTTP"
    capture: Literal["DISABLED", "SYNTHETIC_ARTIFACTS"] = "DISABLED"
    consent_id: UUID | None = None
    consent_hash: str | None = None
    capture_scopes: CaptureScopes | None = None
    participant: Literal["SYNTHETIC_OFFLINE", "LIVE_HUMAN_MICROPHONE_CAPTURE_OFF"] = "SYNTHETIC_OFFLINE"
    live_grant_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    participant_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    participant_email: str | None = Field(default=None, exclude_if=lambda value: value is None)
    max_duration_seconds: int | None = Field(default=None, exclude_if=lambda value: value is None)


class VoiceInvocationView(StrictModel):
    id: UUID
    native_id: str
    tool: str
    arguments: dict[str, object]
    result: dict[str, object] | None
    action_id: UUID | None
    created_at: datetime


class VoiceEventView(StrictModel):
    id: UUID
    kind: str
    created_at: datetime


class VoiceSourceView(StrictModel):
    invoice_id: UUID
    source_revision: str
    captured_at: datetime


class VoiceOutcomeView(StrictModel):
    invocation_id: UUID
    action_id: UUID
    invoice_id: UUID
    record_kind: Literal["task", "case", "promise", "hold"]
    changed_ids: list[UUID]


VoiceStopOutcome = Literal["NOT_REQUESTED", "RESERVED", "ATTEMPTED", "ACCEPTED", "UNKNOWN", "FAILED"]


class VoiceSessionView(StrictModel):
    id: UUID
    version: int
    approval_id: UUID
    connection_id: UUID
    invoice_ids: list[UUID]
    objective: str
    demo_scenario: Literal["task", "business_facts"] = "task"
    business_evidence: list[VoiceEvidenceView] = Field(default_factory=list)
    state: str
    provider_call_id: str | None
    evidence_mode: Literal["MOCKED_HTTP", "LIVE"] = "MOCKED_HTTP"
    consent: VoiceConsentView | None = None
    communication_id: UUID | None = None
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    capture_evidence: Literal["FAKE"] | None = None
    transcript_available: bool = False
    recording_available: bool = False
    live_status: Literal["BLOCKED", "SELECTED"] = "BLOCKED"
    participant_id: UUID | None = None
    participant_email: str | None = None
    can_join: bool = False
    join_reasons: list[str] = Field(default_factory=list)
    stop_outcome: VoiceStopOutcome = "NOT_REQUESTED"
    can_end: bool = False
    end_reasons: list[str] = Field(default_factory=list)
    pstn_status: Literal["SKIPPED"] = "SKIPPED"
    expires_at: datetime
    changed_ids: list[UUID]
    outcomes: list[VoiceOutcomeView] = Field(default_factory=list)
    invocations: list[VoiceInvocationView] = Field(default_factory=list)
    events: list[VoiceEventView] = Field(default_factory=list)
    source_scope: list[VoiceSourceView] = Field(default_factory=list)
    can_reconcile: bool = False
    reconciliation_reasons: list[str] = Field(default_factory=list)
    can_start: bool = False
    reasons: list[str] = Field(default_factory=list)


class SetupVoice(StrictModel):
    request_id: Annotated[UUID, Field(strict=False)]


class StartVoice(StrictModel):
    expected_version: int = Field(ge=1)


class VoiceConnectionView(StrictModel):
    id: UUID
    label: str


class VoicePage(StrictModel):
    evidence_mode: Literal["MOCKED_HTTP", "LIVE"] = "MOCKED_HTTP"
    can_setup: bool = False
    setup_reasons: list[str] = Field(default_factory=list)
    items: list[VoiceSessionView]
    connections: list[VoiceConnectionView]
    can_prepare: bool
    reasons: list[str]


class VoiceJoinView(StrictModel):
    session_id: UUID
    provider_call_id: str
    web_call_url: str
    expires_at: datetime
