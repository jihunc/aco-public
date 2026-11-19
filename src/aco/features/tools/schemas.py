"""Exact scoped read schema; no caller-selected tenant, SQL, operation or mutation fields."""

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from aco.core.types import StrictModel
from aco.features.tools.task_update import TaskFact
from aco.features.voice.evidence_dto import VoiceOperationalEvidence


class InvoiceQuery(StrictModel):
    invoice_id: UUID


class SourceStamp(StrictModel):
    provider_connection_id: UUID
    external_id: str
    source_revision: str
    observed_at: AwareDatetime
    effective_at: AwareDatetime | None = None
    synthetic: bool
    freshness: Literal["LAST_KNOWN_SNAPSHOT"] = "LAST_KNOWN_SNAPSHOT"


class InvoiceFact(StrictModel):
    kind: Literal["invoice"] = "invoice"
    invoice_id: UUID
    customer_id: UUID
    number: str
    balance_minor: int
    currency: str
    exponent: int
    financial_status: str
    due_date: date
    source: SourceStamp


class PaymentFact(StrictModel):
    kind: Literal["payment"] = "payment"
    payment_id: UUID
    allocation_kind: str
    minor_units: int
    currency: str
    source: SourceStamp


class CustomerFact(StrictModel):
    kind: Literal["customer"] = "customer"
    customer_id: UUID
    name: str
    source: SourceStamp


class PolicyFact(StrictModel):
    kind: Literal["policy"] = "policy"
    policy_id: UUID
    version_id: UUID
    version: int
    state: str
    definition: dict[str, object]
    updated_at: AwareDatetime
    synthetic: bool


class CustomerInvoiceFact(StrictModel):
    kind: Literal["customer_invoice"] = "customer_invoice"
    invoice: InvoiceFact
    verified: bool
    action_scope: Literal["CONTEXT_ONLY"] = "CONTEXT_ONLY"


class RecipientStatusFact(StrictModel):
    contact_id: UUID
    source_revision: str
    control_id: UUID | None
    control_version: int | None
    destination_present: bool
    destination_verified: bool
    review_matches_current_source: bool
    opt_out: bool
    allowed_channels: list[str]
    timezone: str | None
    timezone_verified: bool
    owner_confirmed_tenant_timezone: bool


class PolicyContactFact(StrictModel):
    kind: Literal["policy_contact_status"] = "policy_contact_status"
    invoice_id: UUID
    customer_id: UUID
    provider_connection_id: UUID
    observed_at: AwareDatetime
    policy: PolicyFact | None
    policy_state: Literal["ACTIVE", "UNAVAILABLE"]
    collection_lifecycle: str | None
    hold_reasons: list[str] = Field(max_length=50)
    active_hold_count: int
    hold_details_truncated: bool
    unresolved_customer_scope_reviews: int
    pending_contact_stops: int
    recipient: RecipientStatusFact | None
    customer_override_opt_out: bool | None
    customer_override_allowed_channels: list[str] | None
    verified_email_recipient_available: bool
    customer_exposure_24h: int
    customer_exposure_7d: int
    recipient_exposure_24h: int
    recipient_exposure_7d: int
    unknown_exposure_7d: int
    cap_24h: int | None
    cap_7d: int | None
    outbound_paused: bool
    automation_enabled: bool
    source: SourceStamp
    snapshot_observed_within_15m: bool
    source_freshness_basis: Literal["LAST_KNOWN_SNAPSHOT_NOT_DISPATCH_READ"] = (
        "LAST_KNOWN_SNAPSHOT_NOT_DISPATCH_READ"
    )
    dispatch_authority: Literal["NOT_GRANTED"] = "NOT_GRANTED"


class CaseEvidenceFact(StrictModel):
    kind: Literal["case_evidence"] = "case_evidence"
    evidence_id: UUID
    case_id: UUID
    case_version: int
    evidence_kind: str
    source_kind: Literal["communication", "voice_operational"]
    communication_id: UUID | None = None
    voice_source: VoiceOperationalEvidence | None = None
    source_sha256: str | None = None
    source_state: str
    observed_at: AwareDatetime
    synthetic: bool
    available: bool
    unavailable_reason: Literal["CURRENT_SOURCE_UNAVAILABLE"] | None = None
    excerpt: str | None = Field(default=None, max_length=2000)
    offset: int | None = None
    text_truncated: bool = False


class CommunicationFact(StrictModel):
    communication_version: int | None = None
    source_sha256: str | None = None
    kind: Literal["communication"] = "communication"
    communication_id: UUID
    direction: str
    channel: str
    event_at: AwareDatetime
    delivery_state: str
    synthetic: bool
    provenance: str
    source_state: str
    excerpt: str = Field(max_length=2000)
    offset: int = 0
    text_truncated: bool


class CaseFact(StrictModel):
    kind: Literal["case"] = "case"
    case_id: UUID
    customer_id: UUID
    version: int
    issue_type: str
    claim_key: str
    substance: str
    status: str
    synthetic: bool


class PromiseFact(StrictModel):
    kind: Literal["promise"] = "promise"
    promise_id: UUID
    customer_id: UUID
    version: int
    status: str
    source_kind: Literal["communication", "voice_operational"] = "communication"
    source_communication_id: UUID | None
    voice_source: VoiceOperationalEvidence | None = None
    source_text: str
    source_state: str
    synthetic: bool

    @model_validator(mode="after")
    def exact_source(self) -> "PromiseFact":
        if self.source_kind == "voice_operational":
            if (
                self.source_communication_id is not None
                or self.voice_source is None
                or self.source_state != "VOICE_OPERATIONAL_METADATA"
            ):
                raise ValueError("Voice promise requires its exclusive operational source")
        elif self.source_communication_id is None or self.voice_source is not None:
            raise ValueError("Communication promise requires its exclusive original reference")
        return self


class ReadEvidence(StrictModel):
    step_id: UUID
    invoice_id: UUID
    available: bool
    error_code: Literal["CAPABILITY_UNAVAILABLE"] | None = None
    records: list[
        InvoiceFact
        | PaymentFact
        | CustomerFact
        | PolicyFact
        | CommunicationFact
        | CaseFact
        | PromiseFact
        | CustomerInvoiceFact
        | PolicyContactFact
        | CaseEvidenceFact
        | TaskFact
    ] = Field(max_length=50)
    truncated: bool = False
    as_of: AwareDatetime
    basis: Literal["LAST_KNOWN_SNAPSHOT"] = "LAST_KNOWN_SNAPSHOT"
