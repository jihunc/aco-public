"""Current immutable authorization material shared by proposal and review checks."""

from sqlalchemy.orm import Session

from aco.features.communications.email_live import config_for, current_email_live_grant
from aco.features.communications.models import EmailExecutionActivation
from aco.features.communications.provider_outbound import persisted_outbound
from aco.features.review.email_dto import LiveEmailCostScope


def live_email_cost_scope(db: Session, activation: EmailExecutionActivation) -> LiveEmailCostScope | None:
    if activation.provider_binding is None:
        return None
    capability = persisted_outbound(db, activation)
    if capability.evidence_mode != "LIVE":
        return None
    assert capability.live_grant_id is not None  # persisted_outbound already validates exact live material.
    grant = current_email_live_grant(
        db, capability.live_grant_id, activation.tenant_id, activation.email_connection_id
    )
    config = config_for(grant)
    return LiveEmailCostScope(
        grant_id=grant.id,
        grant_version=grant.version,
        max_usd=format(config.max_usd, "f"),
        max_requests=config.max_requests,
        max_messages=config.max_messages,
        max_received_messages=config.max_received_messages,
        request_usd_ceiling=format(config.request_usd_ceiling, "f"),
        price_source_ref=config.price_source_ref,
        price_source_at=config.price_source_at,
    )
