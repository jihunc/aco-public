"""Voice producer attribution through the canonical usage contract; no new ledger."""

from uuid import UUID

from sqlalchemy.orm import Session

from aco.core.types import CostQuality, ProviderCost
from aco.features.usage.dto import ProviderUsageInput, Units
from aco.features.usage.models import UsageAttempt, UsageSource
from aco.features.usage.service import UsageContext, UsageService


def record_voice_usage(
    db: Session,
    ctx: UsageContext,
    attempt: UsageAttempt,
    *,
    component: str,
    operation: str,
    outcome: str,
    native_operation_id: str | None = None,
    session_id: UUID | None = None,
) -> None:
    """Called only for a retained, independently authorized actual transport intent."""
    event = UsageService(db).record_verified(
        ctx,
        ProviderUsageInput(
            provider_connection_id=attempt.provider_connection_id,
            operation_key=str(attempt.id),
            component=component,
            operation=operation,
            provider_kind="voice",
            provider="vapi",
            synthetic=True,
            cost=ProviderCost(amount=None, currency="USD", quality=CostQuality.UNKNOWN),
            units=Units(requests=1),
            native_operation_id=native_operation_id,
            outcome=outcome,
        ),
    )
    attempt.event_id = event.id
    if session_id is not None:
        db.add(
            UsageSource(
                event_id=event.id, source_kind="voice_session", source_id=session_id, component=component
            )
        )
