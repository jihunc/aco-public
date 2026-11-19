"""Tenant-bound communication identity and immutable dispatch/capture evidence.

Raw originals live in Artifact storage, never in a business JSON/blob ledger.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from aco.features.identity.models import Base, Record


def scoped(column: str, table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(["tenant_id", column], [f"{table}.tenant_id", f"{table}.id"])


class EmailExecutionActivation(Record, Base):
    __tablename__ = "email_execution_activations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        scoped("email_connection_id", "provider_connections"),
        scoped("storage_connection_id", "provider_connections"),
        ForeignKeyConstraint(["tenant_id", "reviewer_id"], ["memberships.tenant_id", "memberships.user_id"]),
        CheckConstraint("request_cap > 0 AND request_cap <= 100"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    email_connection_id: Mapped[UUID]
    email_connection_version: Mapped[int]
    storage_connection_id: Mapped[UUID]
    storage_connection_version: Mapped[int]
    reviewer_id: Mapped[UUID]
    membership_version: Mapped[int]
    sender: Mapped[str]
    destinations: Mapped[list[str]] = mapped_column(JSON)
    request_cap: Mapped[int]
    capture_policy: Mapped[str]
    provider_binding: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    evidence: Mapped[str]
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EmailActivationRevocation(Record, Base):
    __tablename__ = "email_activation_revocations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "activation_id"),
        scoped("activation_id", "email_execution_activations"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    activation_id: Mapped[UUID]
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("identity_actors.id"))
    reason: Mapped[str]


class CommunicationThread(Record, Base):
    __tablename__ = "communication_threads"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        Index(
            "uq_invoice_outbound_thread",
            "tenant_id",
            "provider_connection_id",
            "origin_invoice_id",
            unique=True,
            postgresql_where=text("purpose = 'invoice_outbound'"),
        ),
        UniqueConstraint("tenant_id", "provider_connection_id", "native_thread_id"),
        scoped("provider_connection_id", "provider_connections"),
        ForeignKeyConstraint(
            ["tenant_id", "origin_invoice_id", "customer_id"],
            [
                "invoice_customer_associations.tenant_id",
                "invoice_customer_associations.invoice_id",
                "invoice_customer_associations.customer_id",
            ],
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    provider_connection_id: Mapped[UUID]
    origin_invoice_id: Mapped[UUID | None]
    customer_id: Mapped[UUID | None]
    purpose: Mapped[str] = mapped_column(default="invoice_outbound", server_default="invoice_outbound")
    native_thread_id: Mapped[str | None]


class Communication(Record, Base):
    __tablename__ = "communications"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "operation_id"),
        UniqueConstraint("tenant_id", "provider_connection_id", "native_message_id"),
        scoped("provider_connection_id", "provider_connections"),
        scoped("thread_id", "communication_threads"),
        scoped("origin_invoice_id", "invoice_snapshots"),
        CheckConstraint("direction IN ('incoming','outgoing')"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    provider_connection_id: Mapped[UUID]
    thread_id: Mapped[UUID]
    origin_invoice_id: Mapped[UUID | None]
    operation_id: Mapped[UUID]
    native_message_id: Mapped[str | None]
    message_sha256: Mapped[str | None]
    headers: Mapped[list[list[str]]] = mapped_column(JSON, default=list, server_default="[]")
    direction: Mapped[str] = mapped_column(default="outgoing")
    channel: Mapped[str] = mapped_column(default="email")
    kind: Mapped[str] = mapped_column(default="MESSAGE")
    synthetic: Mapped[bool] = mapped_column(default=True)
    content: Mapped[dict[str, object]] = mapped_column(JSON)
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    delivery_state: Mapped[str] = mapped_column(default="prepared")
    source_state: Mapped[str] = mapped_column(default="pending")


class CommunicationInvoiceLink(Record, Base):
    __tablename__ = "communication_invoice_links"
    __table_args__ = (
        UniqueConstraint("tenant_id", "communication_id", "invoice_id", "claim_or_context_key"),
        scoped("communication_id", "communications"),
        scoped("invoice_id", "invoice_snapshots"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    communication_id: Mapped[UUID]
    invoice_id: Mapped[UUID]
    claim_or_context_key: Mapped[str]
    provenance: Mapped[str]
    confidence: Mapped[str]
    evidence_excerpt: Mapped[str]
    evidence_offset: Mapped[int]
    classification: Mapped[str]


class CommunicationSource(Record, Base):
    __tablename__ = "communication_sources"
    __table_args__ = (
        UniqueConstraint("tenant_id", "communication_id", "representation", "sha256"),
        scoped("communication_id", "communications"),
        scoped("artifact_id", "artifacts"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    communication_id: Mapped[UUID]
    artifact_id: Mapped[UUID]
    representation: Mapped[str]
    sha256: Mapped[str]
    evidence: Mapped[dict[str, object]] = mapped_column(JSON)


class EmailDispatchBinding(Record, Base):
    __tablename__ = "email_dispatch_bindings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "action_id"),
        UniqueConstraint("tenant_id", "operation_id"),
        scoped("action_id", "scheduled_actions"),
        scoped("communication_id", "communications"),
        scoped("activation_id", "email_execution_activations"),
        scoped("email_connection_id", "provider_connections"),
        scoped("storage_connection_id", "provider_connections"),
        scoped("contact_reservation_id", "contact_reservations"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    action_id: Mapped[UUID]
    communication_id: Mapped[UUID]
    activation_id: Mapped[UUID]
    email_connection_id: Mapped[UUID]
    storage_connection_id: Mapped[UUID]
    operation_id: Mapped[UUID]
    operation_key: Mapped[str]
    payload_hash: Mapped[str]
    source_revision: Mapped[str]
    command: Mapped[dict[str, object]] = mapped_column(JSON)
    mime_boundary: Mapped[str]
    mime_sha256: Mapped[str]
    contact_reservation_id: Mapped[UUID] = mapped_column(ForeignKey("contact_reservations.id"))


class EmailSubmission(Record, Base):
    """Prepared row commits before authority locks; independent intent never locks action."""

    __tablename__ = "email_submissions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "operation_id"),
        scoped("binding_id", "email_dispatch_bindings"),
        scoped("usage_attempt_id", "usage_attempts"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    binding_id: Mapped[UUID]
    operation_id: Mapped[UUID]
    state: Mapped[str] = mapped_column(default="prepared")
    intent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None]
    provider_request_id: Mapped[str | None]
    failure: Mapped[dict[str, object] | None] = mapped_column(JSON)
    usage_attempt_id: Mapped[UUID | None] = mapped_column(ForeignKey("usage_attempts.id"))


class CommunicationCaptureGrant(Record, Base):
    __tablename__ = "communication_capture_grants"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        scoped("binding_id", "email_dispatch_bindings"),
        scoped("communication_id", "communications"),
        scoped("actor_id", "identity_actors"),
        scoped("storage_connection_id", "provider_connections"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    actor_id: Mapped[UUID]
    actor_version: Mapped[int]
    tenant_generation: Mapped[int]
    binding_id: Mapped[UUID]
    communication_id: Mapped[UUID]
    storage_connection_id: Mapped[UUID]
    correlation_id: Mapped[UUID]
    action_version: Mapped[int]
    job_id: Mapped[int]
    lease_token: Mapped[UUID]
    source_revision: Mapped[str]
    objects: Mapped[list[dict[str, object]]] = mapped_column(JSON)
    capture_policy: Mapped[str]
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CaptureGrantRevocation(Record, Base):
    __tablename__ = "capture_grant_revocations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "grant_id"),
        scoped("grant_id", "communication_capture_grants"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    grant_id: Mapped[UUID]
    reason: Mapped[str]


class CommunicationDeliveryEvent(Record, Base):
    __tablename__ = "communication_delivery_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "provider_connection_id", "event_key"),
        scoped("communication_id", "communications"),
        scoped("provider_connection_id", "provider_connections"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    communication_id: Mapped[UUID]
    provider_connection_id: Mapped[UUID]
    event_key: Mapped[str]
    state: Mapped[str]
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    evidence: Mapped[dict[str, object]] = mapped_column(JSON)


class EmailIngressActivation(Record, Base):
    __tablename__ = "email_ingress_activations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        scoped("email_connection_id", "provider_connections"),
        scoped("storage_connection_id", "provider_connections"),
        scoped("worker_actor_id", "identity_actors"),
        ForeignKeyConstraint(
            ["tenant_id", "enabling_member_id"], ["memberships.tenant_id", "memberships.user_id"]
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    email_connection_id: Mapped[UUID]
    email_connection_version: Mapped[int]
    storage_connection_id: Mapped[UUID]
    storage_connection_version: Mapped[int]
    enabling_member_id: Mapped[UUID]
    membership_version: Mapped[int]
    worker_actor_id: Mapped[UUID]
    actor_version: Mapped[int]
    tenant_generation: Mapped[int]
    secret_sha256: Mapped[str]
    provider_binding: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    evidence: Mapped[str]
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmailIngressReceipt(Record, Base):
    __tablename__ = "email_ingress_receipts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "email_connection_id", "event_id"),
        scoped("activation_id", "email_ingress_activations"),
        scoped("email_connection_id", "provider_connections"),
        scoped("communication_id", "communications"),
        scoped("agent_run_id", "agent_runs"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    activation_id: Mapped[UUID]
    email_connection_id: Mapped[UUID]
    event_id: Mapped[str]
    message_id: Mapped[str]
    raw_sha256: Mapped[str]
    notification: Mapped[dict[str, object]] = mapped_column(JSON)
    signed_raw_body: Mapped[str]
    signature_headers: Mapped[dict[str, str]] = mapped_column(JSON)
    correlation_id: Mapped[UUID]
    state: Mapped[str] = mapped_column(default="queued")
    job_id: Mapped[int | None]
    job_version: Mapped[int] = mapped_column(default=1)
    lease_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    communication_id: Mapped[UUID | None]
    agent_run_id: Mapped[UUID | None]
    error_code: Mapped[str | None]
    review_required: Mapped[bool] = mapped_column(default=True)
    capture_objects: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)


class EmailMailboxClaim(Record, Base):
    """One globally active provider-mailbox owner; released claims preserve provenance."""

    __tablename__ = "email_mailbox_claims"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        scoped("activation_id", "email_ingress_activations"),
        scoped("email_connection_id", "provider_connections"),
        CheckConstraint("address=lower(address) AND length(address) BETWEEN 3 AND 320"),
        Index(
            "email_mailbox_one_active_owner",
            "address",
            unique=True,
            postgresql_where=text("released_at IS NULL"),
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    activation_id: Mapped[UUID]
    email_connection_id: Mapped[UUID]
    address: Mapped[str] = mapped_column(Text)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmailMailboxConflict(Record, Base):
    """Legacy overlapping ownership is quarantined until every original claim is inactive."""

    __tablename__ = "email_mailbox_conflicts"
    address: Mapped[str] = mapped_column(Text, unique=True)
    activation_ids: Mapped[list[str]] = mapped_column(JSON)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmailWebhookAdmission(Record, Base):
    """Shared endpoint event remains bound to the original owner after reassignment."""

    __tablename__ = "email_webhook_admissions"
    __table_args__ = (
        UniqueConstraint("endpoint_id", "event_id"),
        UniqueConstraint("account_id", "event_id"),
        scoped("activation_id", "email_ingress_activations"),
        scoped("email_connection_id", "provider_connections"),
        scoped("receipt_id", "email_ingress_receipts"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    endpoint_id: Mapped[UUID]
    account_id: Mapped[str] = mapped_column(Text)
    event_id: Mapped[str] = mapped_column(Text)
    recipients: Mapped[list[str]] = mapped_column(JSON)
    raw_sha256: Mapped[str] = mapped_column(Text)
    activation_id: Mapped[UUID]
    email_connection_id: Mapped[UUID]
    receipt_id: Mapped[UUID]


class CommunicationRfcIdentity(Record, Base):
    """Append-only provider-read identity; composed originals/content are never rewritten."""

    __tablename__ = "communication_rfc_identities"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("usage_event_id"),
        scoped("provider_connection_id", "provider_connections"),
        scoped("communication_id", "communications"),
        scoped("submission_id", "email_submissions"),
        scoped("usage_event_id", "provider_usage_events"),
        CheckConstraint("length(rfc_message_id) BETWEEN 5 AND 998"),
        CheckConstraint("observation_origin IN ('PROVIDER','APPLICATION')"),
        CheckConstraint("native_identity IN ('PROVIDER','SIMULATED')"),
        CheckConstraint("(observation_origin='PROVIDER') OR provider_request_id IS NULL"),
        CheckConstraint(
            "(synthetic AND native_identity='SIMULATED') OR (NOT synthetic AND native_identity='PROVIDER')"
        ),
        Index("ix_communication_rfc_lookup", "tenant_id", "provider_connection_id", "rfc_message_id"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    provider_connection_id: Mapped[UUID]
    communication_id: Mapped[UUID]
    submission_id: Mapped[UUID]
    usage_event_id: Mapped[UUID]
    provider: Mapped[str] = mapped_column(Text)
    native_message_id: Mapped[str] = mapped_column(Text)
    rfc_message_id: Mapped[str] = mapped_column(Text)
    observation_reference: Mapped[str] = mapped_column(Text)
    observation_origin: Mapped[str] = mapped_column(Text)
    provider_request_id: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    native_identity: Mapped[str] = mapped_column(Text)
    synthetic: Mapped[bool]
