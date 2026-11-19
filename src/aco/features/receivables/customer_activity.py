"""Metadata-only customer activity; retained originals and contact exposures are separate facts."""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.core.types import StrictModel
from aco.features.communications.models import Communication
from aco.features.communications.queries import CommunicationQueries
from aco.features.policies.models import ContactReservation


class InteractionCount(StrictModel):
    channel: str
    direction: str
    state: str
    synthetic: bool
    count: int


class CustomerInteractions(StrictModel):
    as_of_at: datetime
    window_start: datetime
    window_days: int = 30
    original_count: int
    incoming_count: int
    outgoing_count: int
    groups: list[InteractionCount]


class CustomerContactFrequency(StrictModel):
    as_of_at: datetime
    window_start: datetime
    last_24_hours: int
    last_7_days: int
    states: dict[str, int]


def customer_activity(
    db: Session, ctx: TenantContext, customer_id: UUID, at: datetime
) -> tuple[CustomerInteractions, CustomerContactFrequency]:
    # The maintained reader rechecks current human membership. No model/tool context promotion.
    reader = CommunicationQueries(db, ctx)
    start = at - timedelta(days=30)
    groups = db.execute(
        select(
            Communication.channel,
            Communication.direction,
            Communication.delivery_state,
            Communication.synthetic,
            func.count(),
        )
        .where(
            Communication.tenant_id == ctx.tenant_id,
            reader.customer_scope(customer_id),
            Communication.event_at > start,
            Communication.event_at <= at,
            Communication.created_at <= at,
        )
        .group_by(
            Communication.channel,
            Communication.direction,
            Communication.delivery_state,
            Communication.synthetic,
        )
        .order_by(
            Communication.channel,
            Communication.direction,
            Communication.delivery_state,
            Communication.synthetic,
        )
    ).all()
    interactions = CustomerInteractions(
        as_of_at=at,
        window_start=start,
        original_count=sum(row[4] for row in groups),
        incoming_count=sum(row[4] for row in groups if row[1] == "incoming"),
        outgoing_count=sum(row[4] for row in groups if row[1] == "outgoing"),
        groups=[
            InteractionCount(channel=c, direction=d, state=s, synthetic=y, count=n)
            for c, d, s, y, n in groups
        ],
    )
    instant = func.coalesce(ContactReservation.submitted_at, ContactReservation.reserved_at)
    states = dict.fromkeys(("reserved", "inflight", "accepted", "connected", "unknown_outcome"), 0)
    exposure_rows = db.execute(
        select(ContactReservation.status, func.count(), func.count().filter(instant > at - timedelta(days=1)))
        .where(
            ContactReservation.tenant_id == ctx.tenant_id,
            ContactReservation.customer_id == customer_id,
            ContactReservation.status.in_(states),
            instant > at - timedelta(days=7),
            instant <= at,
            ContactReservation.created_at <= at,
        )
        .group_by(ContactReservation.status)
    ).all()
    for state, count, _daily in exposure_rows:
        states[state] = count
    return interactions, CustomerContactFrequency(
        as_of_at=at,
        window_start=at - timedelta(days=7),
        last_24_hours=sum(row[2] for row in exposure_rows),
        last_7_days=sum(row[1] for row in exposure_rows),
        states=states,
    )
