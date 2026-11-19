"""Synthetic-only ingress configuration, observable receipts and exact agent producer scope."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from aco.core.types import StrictModel


class ActivateIngress(StrictModel):
    email_connection_id: Annotated[UUID, Field(strict=False)]
    storage_connection_id: Annotated[UUID, Field(strict=False)]
    evidence: str = Field(min_length=10, max_length=1000)
    expires_in_seconds: int = Field(default=3600, ge=1, le=86400)


class IngressActivationView(StrictModel):
    id: UUID
    version: int
    email_connection_id: UUID
    storage_connection_id: UUID
    expires_at: datetime
    revoked_at: datetime | None
    synthetic: bool = True


class IngressReceiptView(StrictModel):
    synthetic: bool = True
    id: UUID
    state: str
    version: int
    communication_id: UUID | None
    agent_run_id: UUID | None
    job_id: int | None
    error_code: str | None
    review_required: bool
    sender_authority: Literal["UNVERIFIED"] = "UNVERIFIED"
    sensitive_action_allowed: Literal[False] = False


class InboundAgentSource(StrictModel):
    """References to actual persisted records; consuming producer must freshly validate all."""

    receipt_id: UUID
    communication_id: UUID
    activation_id: UUID
    tenant_id: UUID
    enabling_member_id: UUID
    membership_version: int
    worker_actor_id: UUID
    invoice_ids: tuple[UUID, ...]
    correlation_id: UUID
    mode: Literal["FAKE"] = "FAKE"


class RetryIngress(StrictModel):
    expected_version: int = Field(ge=1)


class SimulateIngress(StrictModel):
    request_id: Annotated[UUID, Field(strict=False)]
    expected_activation_version: int = Field(ge=1)
    invoice_ids: list[Annotated[UUID, Field(strict=False)]] = Field(min_length=1, max_length=3)
    scenario: Literal[
        "invoice_reply",
        "promise_reply",
        "ambiguous_promise_reply",
        "multi_promise_reply",
        "multi_promise_amendment",
        "installment_promise_reply",
        "case_reply",
        "case_resolution",
        "contact_stop_reply",
        "three_intent_reply",
        "unlinked_reply",
        "unverified_reference_reply",
        "outbound_echo",
    ]


class IngressConnectionView(StrictModel):
    id: UUID
    provider: str
    status: str


class IngressSetupView(StrictModel):
    enabled: bool
    can_prepare: bool
    email_connections: list[IngressConnectionView]
    storage_connections: list[IngressConnectionView]
    activations: list[IngressActivationView]


class PrepareIngress(StrictModel):
    request_id: Annotated[UUID, Field(strict=False)]
    evidence: str = Field(min_length=10, max_length=1000)
