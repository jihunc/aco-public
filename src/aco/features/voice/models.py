"""Scoped session authority and retained callback/action identities; never verbatim capture."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from aco.features.identity.models import Base, Record


class VoiceSession(Record, Base):
    __tablename__ = "voice_sessions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "request_id"),
        UniqueConstraint("connection_id", "provider_call_id"),
        ForeignKeyConstraint(
            ["tenant_id", "live_grant_id"], ["voice_live_grants.tenant_id", "voice_live_grants.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"], ["provider_connections.tenant_id", "provider_connections.id"]
        ),
    )
    live_grant_id: Mapped[UUID | None]
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    request_id: Mapped[UUID]
    connection_id: Mapped[UUID]
    connection_version: Mapped[int]
    credential_revision: Mapped[str]
    binding_hash: Mapped[str]
    initiator_id: Mapped[UUID] = mapped_column(ForeignKey("identity_users.id"))
    membership_version: Mapped[int]
    tenant_version: Mapped[int]
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("identity_actors.id"))
    actor_version: Mapped[int]
    correlation_id: Mapped[UUID]
    command: Mapped[dict[str, object]] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(default="awaiting_approval")
    provider_call_id: Mapped[str | None]
    approval_id: Mapped[UUID | None] = mapped_column(ForeignKey("approval_requests.id"))
    usage_attempt_id: Mapped[UUID | None] = mapped_column(ForeignKey("usage_attempts.id"))


class VoiceInvoiceLink(Record, Base):
    __tablename__ = "voice_invoice_links"
    __table_args__ = (
        UniqueConstraint("tenant_id", "session_id", "invoice_id"),
        ForeignKeyConstraint(["tenant_id", "session_id"], ["voice_sessions.tenant_id", "voice_sessions.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "invoice_id"], ["invoice_snapshots.tenant_id", "invoice_snapshots.id"]
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    session_id: Mapped[UUID]
    invoice_id: Mapped[UUID]
    source_revision: Mapped[str]
    accounting_connection_id: Mapped[UUID] = mapped_column(ForeignKey("provider_connections.id"))
    connection_version: Mapped[int]


class VoiceInvocation(Record, Base):
    __tablename__ = "voice_invocations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "session_id", "id"),
        UniqueConstraint("session_id", "native_id"),
        ForeignKeyConstraint(["tenant_id", "session_id"], ["voice_sessions.tenant_id", "voice_sessions.id"]),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    session_id: Mapped[UUID]
    native_id: Mapped[str]
    tool: Mapped[str]
    arguments: Mapped[dict[str, object]] = mapped_column(JSON)
    result: Mapped[dict[str, object] | None] = mapped_column(JSON)
    action_id: Mapped[UUID | None] = mapped_column(ForeignKey("scheduled_actions.id"))


class VoiceEventRecord(Record, Base):
    __tablename__ = "voice_events"
    __table_args__ = (
        UniqueConstraint("session_id", "event_key"),
        UniqueConstraint("tenant_id", "session_id", "id"),
        ForeignKeyConstraint(["tenant_id", "session_id"], ["voice_sessions.tenant_id", "voice_sessions.id"]),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    session_id: Mapped[UUID]
    event_key: Mapped[str]
    kind: Mapped[str]
    raw_sha256: Mapped[str]


class VoiceBusinessEvidence(Record, Base):
    """Minimal signed operational facts; never a verbatim speech or original-media source."""

    __tablename__ = "voice_business_evidence"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "session_id", "invocation_id"),
        ForeignKeyConstraint(
            ["tenant_id", "session_id", "invocation_id"],
            ["voice_invocations.tenant_id", "voice_invocations.session_id", "voice_invocations.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "session_id", "source_event_id"],
            ["voice_events.tenant_id", "voice_events.session_id", "voice_events.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "action_id"], ["scheduled_actions.tenant_id", "scheduled_actions.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id"],
            ["provider_connections.tenant_id", "provider_connections.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "accounting_connection_id", "customer_id"],
            ["customers.tenant_id", "customers.provider_connection_id", "customers.id"],
        ),
        CheckConstraint("fact_kind IN ('case','promise','contact_stop')"),
        CheckConstraint("source_state='VOICE_OPERATIONAL_METADATA'"),
        CheckConstraint(
            "(synthetic AND evidence_mode='MOCKED_HTTP') OR (NOT synthetic AND evidence_mode='LIVE')",
            name="ck_voice_business_mode",
        ),
        CheckConstraint("facts_hash ~ '^[0-9a-f]{64}$'"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    session_id: Mapped[UUID]
    invocation_id: Mapped[UUID]
    source_event_id: Mapped[UUID]
    action_id: Mapped[UUID]
    provider_connection_id: Mapped[UUID]
    accounting_connection_id: Mapped[UUID]
    customer_id: Mapped[UUID]
    provider_call_id: Mapped[str] = mapped_column(Text)
    native_invocation_id: Mapped[str] = mapped_column(Text)
    fact_kind: Mapped[str] = mapped_column(Text)
    facts: Mapped[dict[str, object]] = mapped_column(JSON)
    facts_hash: Mapped[str] = mapped_column(Text)
    synthetic: Mapped[bool]
    evidence_mode: Mapped[str] = mapped_column(Text)
    source_state: Mapped[str] = mapped_column(Text)

    @property
    def event_at(self) -> datetime:
        return self.created_at
