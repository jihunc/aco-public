"""Independent synthetic provider-owned source; no financial or application send authority."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import JSON, DateTime, ForeignKey, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from aco.features.identity.models import Base, Record


class NativeEmailOperation(Record, Base):
    __tablename__ = "demo_email_operations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "provider_connection_id", "operation_id", "generation"),
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id"],
            ["provider_connections.tenant_id", "provider_connections.id"],
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    provider_connection_id: Mapped[UUID]
    operation_id: Mapped[UUID]
    generation: Mapped[int]
    payload_hash: Mapped[str]
    native_message_id: Mapped[str]
    native_request_id: Mapped[str]
    vendor_key_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    message: Mapped[dict[str, object]] = mapped_column(JSON)


class NativeEmailInvocation(Record, Base):
    __tablename__ = "demo_email_invocations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id"],
            ["provider_connections.tenant_id", "provider_connections.id"],
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    provider_connection_id: Mapped[UUID]
    operation_id: Mapped[UUID]
    method: Mapped[str]
    payload_hash: Mapped[str]
    native_request_id: Mapped[str]
    result: Mapped[str]


class NativeInboundMessage(Record, Base):
    """Independent simulated mailbox original; never an application send/accounting record."""

    __tablename__ = "demo_email_inbound_messages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "provider_connection_id", "native_message_id"),
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id"],
            ["provider_connections.tenant_id", "provider_connections.id"],
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    provider_connection_id: Mapped[UUID]
    native_message_id: Mapped[str]
    message: Mapped[dict[str, object]] = mapped_column(JSON)
    payload_sha256: Mapped[str]
