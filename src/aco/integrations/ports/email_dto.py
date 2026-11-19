"""Provider-neutral email values; object pointers confer no storage access authority."""

import re
from datetime import UTC, datetime
from email.errors import HeaderParseError
from email.headerregistry import Address
from hashlib import sha256
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, field_validator, model_validator

from aco.core.types import ProviderCost, ProviderReference, StrictModel

NonEmpty = Annotated[str, Field(min_length=1)]
EmailKind = Literal["MESSAGE", "CUSTOMER_REPLY", "DELIVERY_RECEIPT", "BOUNCE", "OUTBOUND_ECHO"]
NativeIdentity = Literal["PROVIDER", "SIMULATED"]


def header_value(value: str) -> str:
    if "\r" in value or "\n" in value:
        raise ValueError("Email header values cannot contain line breaks")
    return value


def mailbox(value: str) -> str:
    header_value(value)
    try:
        address = Address(addr_spec=value)
    except (ValueError, HeaderParseError) as error:
        raise ValueError("Expected a single canonical mailbox address") from error
    if not address.username or not address.domain or address.addr_spec != value:
        raise ValueError("Expected a single canonical mailbox address")
    return value


class EmailObjectReference(StrictModel):
    """Opaque canonical artifact or provider object identity; never an arbitrary URL."""

    artifact_id: UUID | None = None
    provider_ref: ProviderReference | None = None

    @model_validator(mode="after")
    def exactly_one_identity(self) -> "EmailObjectReference":
        if (self.artifact_id is None) == (self.provider_ref is None):
            raise ValueError("An object reference requires exactly one typed identity")
        return self


class EmailAttachment(StrictModel):
    filename: NonEmpty
    content_type: NonEmpty
    reference: EmailObjectReference
    integrity_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    size_bytes: Annotated[int, Field(ge=0)] | None = None
    disposition: Literal["attachment", "inline"] = "attachment"
    content_id: str | None = None

    @field_validator("filename", "content_type", "content_id")
    @classmethod
    def safe_headers(cls, value: str | None) -> str | None:
        return header_value(value) if value is not None else None


class EmailSource(StrictModel):
    """Availability/provenance do not imply archival or provider-native MIME.

    ACO_COMPOSED can carry actual library-composed RFC822 bytes or an explicitly
    typed normalized capture. Only a suitable, genuinely supplied representation
    can satisfy later artifact archival; JSON capture is not original-MIME proof.
    """

    model_config = ConfigDict(ser_json_bytes="base64", val_json_bytes="base64")

    kind: Literal["PROVIDER_ORIGINAL", "UNAVAILABLE", "ACO_COMPOSED"]
    content: bytes | None = None
    reference: EmailObjectReference | None = None
    integrity_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    content_type: NonEmpty = "message/rfc822"

    @model_validator(mode="after")
    def truthful_source(self) -> "EmailSource":
        if self.kind == "UNAVAILABLE":
            if self.content is not None or self.reference is not None or self.integrity_sha256 is not None:
                raise ValueError("Unavailable source cannot contain fabricated evidence")
        elif self.content is None and self.reference is None:
            raise ValueError("An available representation needs bytes or an opaque object reference")
        if self.content is not None and sha256(self.content).hexdigest() != self.integrity_sha256:
            raise ValueError("Supplied source bytes require their matching SHA256")
        if self.kind == "ACO_COMPOSED" and self.content is None:
            raise ValueError("A composed representation requires its actual bytes")
        return self


class EmailContent(StrictModel):
    """Captured content is untrusted evidence; only OutboundEmail validates its shape for sending."""

    sender: str | None = None
    to: tuple[str, ...] = ()
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    subject: str
    text_body: str | None = None
    html_body: str | None = None
    attachments: tuple[EmailAttachment, ...] = ()
    message_id: str | None = None
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()
    provider_thread_ref: ProviderReference | None = None


class OutboundEmail(EmailContent):
    sender: NonEmpty
    to: Annotated[tuple[str, ...], Field(min_length=1)]
    tenant_id: UUID
    provider_connection_id: UUID
    origin_invoice_id: UUID
    conversation_thread_id: UUID
    operation_id: UUID

    @field_validator("sender")
    @classmethod
    def valid_sender(cls, value: str) -> str:
        return mailbox(value)

    @field_validator("to", "cc", "bcc")
    @classmethod
    def valid_recipients(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(mailbox(value) for value in values)

    @field_validator("subject")
    @classmethod
    def valid_subject(cls, value: str) -> str:
        return header_value(value)

    @field_validator("message_id", "in_reply_to")
    @classmethod
    def valid_message_id(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"<[^<>\s]+>", value):
            raise ValueError("Expected one RFC message identifier")
        return value

    @field_validator("references")
    @classmethod
    def valid_references(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            cls.valid_message_id(value)
        return values

    @model_validator(mode="after")
    def has_body(self) -> "OutboundEmail":
        if self.text_body is None and self.html_body is None:
            raise ValueError("An email must retain a text or HTML body")
        return self


class EmailMessage(EmailContent):
    """Provider delivery/source authenticity never establishes purported author authority.

    Captured headers are retained evidence, never safe outgoing header instructions.
    Missing/unparseable normalized content is allowed; original availability is explicit.
    """

    tenant_id: UUID
    provider_connection_id: UUID
    provider_ref: ProviderReference
    native_identity: NativeIdentity
    synthetic: bool
    authenticity: Literal["VERIFIED", "UNVERIFIED", "SIMULATED"] = "SIMULATED"
    revision: str | None = None
    direction: Literal["incoming", "outgoing"]
    kind: EmailKind = "MESSAGE"
    headers: tuple[tuple[str, str], ...] = ()
    source: EmailSource
    received_at: AwareDatetime | None = None
    sent_at: AwareDatetime | None = None
    origin_invoice_id: UUID | None = None
    conversation_thread_id: UUID | None = None
    operation_id: UUID | None = None

    @field_validator("received_at", "sent_at")
    @classmethod
    def utc_timestamps(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    @field_validator("headers")
    @classmethod
    def normalized_headers(cls, values: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        return tuple((name.lower(), value) for name, value in values)

    @model_validator(mode="after")
    def coherent_message(self) -> "EmailMessage":
        if self.provider_ref.connection_id != self.provider_connection_id:
            raise ValueError("Message identity must match its connection")
        if self.native_identity == "SIMULATED" and not self.synthetic:
            raise ValueError("Simulated identity must be labeled synthetic")
        if self.authenticity == "SIMULATED" and not self.synthetic:
            raise ValueError("Simulated authenticity must be labeled synthetic")
        if self.direction == "incoming":
            if self.received_at is None or self.sent_at is not None or self.source.kind == "ACO_COMPOSED":
                raise ValueError("Inbound source needs a received time and cannot be ACO-composed")
        elif self.sent_at is None or self.received_at is not None:
            raise ValueError("Outbound source needs its sent time")
        return self


class InboundEmail(EmailMessage):
    direction: Literal["incoming"] = "incoming"
    received_at: AwareDatetime


class EmailObservation(StrictModel):
    tenant_id: UUID
    provider_connection_id: UUID
    provider_ref: ProviderReference
    # Observation IDs created by ACO are never vendor-native request evidence.
    reference_origin: Literal["PROVIDER", "APPLICATION"] = "PROVIDER"
    provider_request_id: NonEmpty | None = None
    correlation_id: UUID
    observed_at: AwareDatetime
    revision: str | None
    native_identity: NativeIdentity
    synthetic: bool
    authenticity: Literal["VERIFIED", "UNVERIFIED", "SIMULATED"]
    attempt: Annotated[int, Field(ge=1)] = 1
    cost: ProviderCost

    @field_validator("observed_at")
    @classmethod
    def utc_observed_at(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def coherent_observation(self) -> "EmailObservation":
        if self.reference_origin == "APPLICATION" and self.provider_request_id is not None:
            raise ValueError("Application observation identity cannot claim a provider request ID")
        if self.provider_ref.connection_id != self.provider_connection_id:
            raise ValueError("Observation identity must match its connection")
        if (self.native_identity == "SIMULATED" or self.authenticity == "SIMULATED") and not self.synthetic:
            raise ValueError("Simulated provenance must be labeled synthetic")
        return self


class EmailResult[T](EmailObservation):
    value: T


class DeliveryAttempt(EmailObservation):
    operation_id: UUID
    status: Literal["ACCEPTED"]
    message_ref: ProviderReference

    @model_validator(mode="after")
    def coherent_message_ref(self) -> "DeliveryAttempt":
        if (self.message_ref.connection_id, self.message_ref.provider) != (
            self.provider_connection_id,
            self.provider_ref.provider,
        ):
            raise ValueError("Delivery message identity must match its connection/provider")
        return self


class EmailSyncCursor(StrictModel):
    tenant_id: UUID
    provider_connection_id: UUID
    token: NonEmpty


class EmailNotification(StrictModel):
    tenant_id: UUID
    provider_connection_id: UUID
    provider_ref: ProviderReference
    message_ref: ProviderReference
    kind: EmailKind
    occurred_at: AwareDatetime
    native_identity: NativeIdentity
    synthetic: bool

    @field_validator("occurred_at")
    @classmethod
    def utc_occurred_at(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def coherent_notification(self) -> "EmailNotification":
        if (self.provider_ref.connection_id, self.message_ref.connection_id) != (
            self.provider_connection_id,
            self.provider_connection_id,
        ) or self.provider_ref.provider != self.message_ref.provider:
            raise ValueError("Notification and message identities must share their provider connection")
        if self.provider_ref.external_id == self.message_ref.external_id:
            raise ValueError("Notification identity differs from message identity")
        if self.native_identity == "SIMULATED" and not self.synthetic:
            raise ValueError("Simulated identity must be labeled synthetic")
        return self


class EmailSyncPage(StrictModel):
    messages: tuple[EmailMessage, ...]
    notifications: tuple[EmailNotification, ...]
    next_cursor: EmailSyncCursor


class MailboxAuthorization(StrictModel):
    operation_id: UUID
    credential_reference: NonEmpty


class EmailConnectionState(StrictModel):
    connected: bool
    mode: Literal["SIMULATED", "AUTHORIZED_MAILBOX", "TRANSACTIONAL"]


class NotificationSubscriptionRequest(StrictModel):
    operation_id: UUID
    subscription_ref: ProviderReference | None = None


class EmailSubscription(StrictModel):
    provider_ref: ProviderReference
    expires_at: AwareDatetime | None

    @field_validator("expires_at")
    @classmethod
    def utc_expires_at(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None
