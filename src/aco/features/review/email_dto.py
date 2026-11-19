"""Server-derived scope for a graph email proposal; never a model authority DTO."""

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from aco.core.types import StrictModel
from aco.features.review.internal_work import ApprovalProposal


class LiveEmailCostScope(StrictModel):
    """Reviewed shared-grant ceiling, never predicted cost or provider billing."""

    grant_id: UUID
    grant_version: int = Field(ge=1)
    currency: Literal["USD"] = "USD"
    max_usd: str
    max_requests: int = Field(ge=1, le=20)
    max_messages: int = Field(ge=1, le=3)
    max_received_messages: int = Field(ge=0, le=3)
    request_usd_ceiling: str
    price_source_ref: str
    price_source_at: AwareDatetime

    @property
    def authorization_ceiling(self) -> str:
        return format(min(Decimal(self.max_usd), self.max_requests * Decimal(self.request_usd_ceiling)), "f")


class AgentEmailApprovalProposal(ApprovalProposal):
    effect: Literal["agent_email"] = "agent_email"
    run_id: UUID
    evidence_step_ids: list[UUID] = Field(min_length=2, max_length=8)
    subject: str = Field(min_length=1, max_length=240)
    rule_id: UUID
    email_activation_id: UUID
    cost_basis: Literal["SYNTHETIC_NO_CHARGE", "SHARED_GRANT_AUTHORIZATION_CEILING"] = "SYNTHETIC_NO_CHARGE"
    live_cost_scope: LiveEmailCostScope | None = None
