"""Identity persistence. Identity credentials stay encrypted in the adapter's tables."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Record:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    version: Mapped[int] = mapped_column(default=1)


class User(Record, Base):
    __tablename__ = "identity_users"
    __table_args__ = (UniqueConstraint("issuer", "subject"),)
    issuer: Mapped[str] = mapped_column(String(512))
    subject: Mapped[str] = mapped_column(String(512))
    email: Mapped[str] = mapped_column(String(320))
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)


class Tenant(Record, Base):
    __tablename__ = "tenants"
    name: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(80))
    is_demo: Mapped[bool] = mapped_column(default=True)
    automation_enabled: Mapped[bool] = mapped_column(default=False)
    outbound_paused: Mapped[bool] = mapped_column(default=True)


class Actor(Record, Base):
    """One identity namespace. Human UUIDs and their historical attribution stay unchanged."""

    __tablename__ = "identity_actors"
    __table_args__ = (
        UniqueConstraint("tenant_id", "purpose"),
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint(
            "(kind='human' AND user_id IS NOT NULL AND user_id=id "
            "AND tenant_id IS NULL AND purpose IS NULL) OR "
            "(kind='service' AND user_id IS NULL AND tenant_id IS NOT NULL AND purpose IS NOT NULL "
            "AND purpose IN ('durable_worker','voice_callback'))"
        ),
    )
    kind: Mapped[str]
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("identity_users.id"), unique=True)
    tenant_id: Mapped[UUID | None] = mapped_column(ForeignKey("tenants.id"))
    purpose: Mapped[str | None]
    active: Mapped[bool] = mapped_column(default=True)


class Membership(Record, Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id"),
        CheckConstraint("role IN ('owner','finance_manager','operator','viewer')"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("identity_users.id"), index=True)
    role: Mapped[str] = mapped_column(String(32))
    active: Mapped[bool] = mapped_column(default=True)


class ProviderConnection(Record, Base):
    __tablename__ = "provider_connections"
    __table_args__ = (UniqueConstraint("tenant_id", "id"),)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    provider_type: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(80))
    external_account_id: Mapped[str | None]
    encrypted_credentials_ref: Mapped[str | None]
    capabilities: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(default="NOT_CONFIGURED")
    sync_cursor: Mapped[str | None]


class AuditEvent(Record, Base):
    __tablename__ = "identity_audit_events"
    __table_args__ = (UniqueConstraint("tenant_id", "id"),)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("identity_actors.id"))
    correlation_id: Mapped[UUID]
    action: Mapped[str]
    target_id: Mapped[UUID]
    details: Mapped[dict[str, str | bool]] = mapped_column(JSON, default=dict)


class Invitation(Record, Base):
    __tablename__ = "identity_invitations"
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    creator_id: Mapped[UUID] = mapped_column(ForeignKey("identity_users.id"))
    email: Mapped[str]
    role: Mapped[str]
    token_hash: Mapped[str] = mapped_column(unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoginAttempt(Record, Base):
    __tablename__ = "identity_login_attempts"
    state: Mapped[str] = mapped_column(unique=True)
    encrypted_payload: Mapped[str]
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed: Mapped[bool] = mapped_column(default=False)


class LoginSession(Record, Base):
    __tablename__ = "identity_sessions"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("identity_users.id"), index=True)
    csrf_token: Mapped[str]
    encrypted_token: Mapped[str]
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(default=False)


class IdentityProviderEvent(Record, Base):
    """Pre-membership identity has no tenant; never fabricate tenant-scoped authority."""

    __tablename__ = "identity_provider_events"
    connection_id: Mapped[UUID]
    correlation_id: Mapped[UUID]
    operation: Mapped[str]
    provider: Mapped[str]
    native_id: Mapped[str | None]
    outcome: Mapped[str]
    attempt: Mapped[int] = mapped_column(default=1)
    # NULL is historical uncertainty; FALSE proves local-only audit, TRUE durable intent.
    provider_attempted: Mapped[bool | None]
    cost_quality: Mapped[str] = mapped_column(default="UNKNOWN")
    cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(38, 12))
