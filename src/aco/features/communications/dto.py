"""Strict public read/configuration contracts and internal persisted-command reference."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from aco.integrations.ports.email_dto import EmailContent, mailbox


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReviewEmailExecution(Command):
    expected_activation_id: UUID | None
    sender: str
    destinations: list[str] = Field(min_length=1, max_length=100)
    request_cap: int = Field(ge=1, le=100)
    synthetic_capture_reviewed: Literal[True]
    sender_and_destinations_reviewed: Literal[True]
    evidence: str = Field(min_length=1, max_length=1000)

    @field_validator("sender")
    @classmethod
    def sender_address(cls, value: str) -> str:
        return mailbox(value)

    @field_validator("destinations")
    @classmethod
    def destination_addresses(cls, values: list[str]) -> list[str]:
        return sorted(set(mailbox(v).casefold() for v in values))


class EmailActivationView(Command):
    id: UUID
    email_connection_id: UUID
    storage_connection_id: UUID
    sender: str
    destinations: list[str]
    request_cap: int
    expires_at: datetime
    reviewer_id: UUID
    evidence: str
    current: bool


class EmailExecutionView(Command):
    system_enabled: bool
    synthetic: bool = True
    mode: Literal["DISABLED", "SYNTHETIC", "MOCKED_HTTP", "LIVE"] = "DISABLED"
    provider: Literal["demo_email", "resend"] | None = None
    activation: EmailActivationView | None


class ProviderEmailConfiguration(Command):
    connection_id: UUID
    expected_activation_id: UUID | None
    reviewed_hash: str
    provider: Literal["resend"] = "resend"
    evidence_mode: Literal["MOCKED_HTTP", "LIVE"]
    mode: Literal["ROUNDTRIP_TEST", "OUTBOUND_TEST_ONLY"]
    sender: str
    destinations: list[str]
    request_cap: int
    expires_at: datetime
    max_requests: int | None = None
    max_usd: str | None = None


class ProviderEmailReviewView(Command):
    available: bool
    configurations: list[ProviderEmailConfiguration]
    reason: str | None = None


class ReviewProviderEmailExecution(Command):
    connection_id: UUID
    expected_activation_id: UUID | None
    reviewed_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence: str = Field(min_length=1, max_length=1000)


class CommunicationFilters(Command):
    invoice_id: UUID | None = None
    customer_id: UUID | None = None
    thread_id: UUID | None = None
    direction: Literal["incoming", "outgoing"] | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=30, ge=1, le=100)


class InvoiceLinkView(Command):
    invoice_id: UUID
    provenance: str
    evidence_excerpt: str
    confidence: str


class SourceView(Command):
    artifact_id: UUID
    representation: str
    sha256: str
    storage_state: str
    available: bool


class DeliveryEventView(Command):
    state: str
    occurred_at: datetime


class CommunicationView(Command):
    id: UUID
    thread_id: UUID
    origin_invoice_id: UUID | None
    provider_connection_id: UUID
    native_message_id: str | None
    direction: str
    channel: str
    kind: str
    synthetic: bool
    event_at: datetime
    delivery_state: str
    source_state: str
    content: EmailContent
    invoice_links: list[InvoiceLinkView]
    sources: list[SourceView]
    delivery_events: list[DeliveryEventView]
    action_id: UUID | None


class CommunicationPage(Command):
    items: list[CommunicationView]
    page: int
    page_size: int
    total: int


class ThreadView(Command):
    id: UUID
    provider_connection_id: UUID
    origin_invoice_id: UUID | None
    customer_id: UUID | None
    native_thread_id: str | None
    messages: list[CommunicationView]


class RunDueCommand(Command):
    action_id: UUID
    expected_version: int = Field(ge=1)


class RunDueResult(Command):
    action_id: UUID
    status: Literal["queued"] = "queued"


class DeliveryReconciliation(Command):
    action_id: UUID
    state: str
    communication_id: UUID | None
    resubmitted: Literal[False] = False


class ApprovedOutboundEmail(Command):
    """Reference only. The common guard reloads and compares exact persisted material."""

    binding_id: UUID
    payload_hash: str
