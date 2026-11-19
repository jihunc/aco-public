"""Private originals, synthetic retention and durable purge requests; no raw body columns."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from aco.features.identity.models import Base, Record


class RetentionPolicy(Record, Base):
    __tablename__ = "artifact_retention_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "policy_key"),
        CheckConstraint(
            "(retention_days = 30 AND synthetic AND approval IS NULL) OR "
            "(NOT synthetic AND retention_days BETWEEN 1 AND 30 AND approval IS NOT NULL)"
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    policy_key: Mapped[str]
    synthetic: Mapped[bool]
    retention_days: Mapped[int]
    authority: Mapped[str]
    approval: Mapped[dict[str, object] | None] = mapped_column(JSON(none_as_null=True))


class Artifact(Record, Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "source_kind", "source_reference"),
        ForeignKeyConstraint(
            ["tenant_id", "provider_connection_id"],
            ["provider_connections.tenant_id", "provider_connections.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "retention_policy_id"],
            ["artifact_retention_policies.tenant_id", "artifact_retention_policies.id"],
        ),
        CheckConstraint(
            "storage_state IN ('pending','stored','failed','quarantined','purge_pending','deleted','blocked')"
        ),
        CheckConstraint("size_bytes >= 0"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    provider_connection_id: Mapped[UUID]
    retention_policy_id: Mapped[UUID]
    source_kind: Mapped[str]
    source_reference: Mapped[str]
    sha256: Mapped[str]
    content_type: Mapped[str]
    size_bytes: Mapped[int]
    capture_class: Mapped[str]
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    storage_state: Mapped[str]
    object_key: Mapped[str]
    object_version: Mapped[str | None]
    encryption: Mapped[str | None]
    failure_code: Mapped[str | None]
    legal_hold_state: Mapped[str] = mapped_column(default="none")
    consent_revoked: Mapped[bool] = mapped_column(default=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ArtifactPurge(Record, Base):
    __tablename__ = "artifact_purge_requests"
    __table_args__ = (
        UniqueConstraint("artifact_id"),
        ForeignKeyConstraint(["tenant_id", "artifact_id"], ["artifacts.tenant_id", "artifacts.id"]),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    artifact_id: Mapped[UUID]
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str]
    reason: Mapped[str]
    failure_code: Mapped[str | None]
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_receipt: Mapped[str | None]
    job_id: Mapped[int | None] = mapped_column(BigInteger)
    job_version: Mapped[int] = mapped_column(default=1, server_default="1")
    worker_actor_id: Mapped[UUID | None] = mapped_column(ForeignKey("identity_actors.id"))
    lease_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
