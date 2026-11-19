"""Immutable consent evidence and separate withdrawal; never raw media or inferred consent."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import JSON, DateTime, ForeignKey, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from aco.features.identity.models import Base, Record


class ConsentEvidence(Record, Base):
    __tablename__ = "consent_evidence"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "request_id"),
        ForeignKeyConstraint(
            ["tenant_id", "accounting_connection_id", "customer_id"],
            ["customers.tenant_id", "customers.provider_connection_id", "customers.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "voice_connection_id"],
            ["provider_connections.tenant_id", "provider_connections.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "storage_connection_id"],
            ["provider_connections.tenant_id", "provider_connections.id"],
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    request_id: Mapped[UUID]
    customer_id: Mapped[UUID]
    accounting_connection_id: Mapped[UUID]
    voice_connection_id: Mapped[UUID]
    voice_connection_version: Mapped[int]
    storage_connection_id: Mapped[UUID | None]
    storage_connection_version: Mapped[int | None]
    recorded_by: Mapped[UUID] = mapped_column(ForeignKey("identity_users.id"))
    membership_version: Mapped[int]
    tenant_version: Mapped[int]
    command: Mapped[dict[str, object]] = mapped_column(JSON)
    material_hash: Mapped[str]
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ConsentRevocation(Record, Base):
    __tablename__ = "consent_revocations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "consent_id"),
        UniqueConstraint("tenant_id", "request_id"),
        ForeignKeyConstraint(
            ["tenant_id", "consent_id"], ["consent_evidence.tenant_id", "consent_evidence.id"]
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    consent_id: Mapped[UUID]
    request_id: Mapped[UUID]
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("identity_users.id"))
    command: Mapped[dict[str, object]] = mapped_column(JSON)


class VoiceArtifactLink(Record, Base):
    __tablename__ = "voice_artifact_links"
    __table_args__ = (
        UniqueConstraint("tenant_id", "session_id", "kind"),
        UniqueConstraint("artifact_id"),
        ForeignKeyConstraint(["tenant_id", "session_id"], ["voice_sessions.tenant_id", "voice_sessions.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "consent_id"], ["consent_evidence.tenant_id", "consent_evidence.id"]
        ),
        ForeignKeyConstraint(["tenant_id", "artifact_id"], ["artifacts.tenant_id", "artifacts.id"]),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    session_id: Mapped[UUID]
    consent_id: Mapped[UUID]
    artifact_id: Mapped[UUID]
    kind: Mapped[str]
    native_reference: Mapped[str]
    provider_purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_purge_receipt: Mapped[str | None]


class VoicePostCall(Record, Base):
    __tablename__ = "voice_post_calls"
    __table_args__ = (
        UniqueConstraint("tenant_id", "session_id"),
        UniqueConstraint("communication_id"),
        ForeignKeyConstraint(["tenant_id", "session_id"], ["voice_sessions.tenant_id", "voice_sessions.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "communication_id"], ["communications.tenant_id", "communications.id"]
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    session_id: Mapped[UUID]
    communication_id: Mapped[UUID]


class ArtifactPrivacyEvent(Record, Base):
    __tablename__ = "artifact_privacy_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "request_id"),
        ForeignKeyConstraint(["tenant_id", "artifact_id"], ["artifacts.tenant_id", "artifacts.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "parent_id"], ["artifact_privacy_events.tenant_id", "artifact_privacy_events.id"]
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    artifact_id: Mapped[UUID]
    request_id: Mapped[UUID]
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("identity_users.id"))
    kind: Mapped[str]
    parent_id: Mapped[UUID | None]
    command: Mapped[dict[str, object]] = mapped_column(JSON)
