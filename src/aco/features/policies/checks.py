"""Current scoped policy queries; callers must establish their own sealed authority first.

These functions grant no permission and accept no human/service context conversion.
The existing authorized PolicyGuards entrypoints retain their authorization ordering.
"""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import or_, select, true
from sqlalchemy.orm import Session

from aco.features.accounting.models import CollectionRecord, InvoiceSnapshot
from aco.features.policies.contact_stop import pending_contact_stops
from aco.features.policies.dto import GuardDecision
from aco.features.policies.models import ContactReservation
from aco.features.promises.models import CustomerScopeReview


def operational_status(db: Session, invoice: InvoiceSnapshot) -> GuardDecision:
    collection = db.scalar(
        select(CollectionRecord)
        .where(CollectionRecord.tenant_id == invoice.tenant_id, CollectionRecord.invoice_id == invoice.id)
        .execution_options(populate_existing=True)
    )
    reasons = []
    if collection is None:
        reasons.append("OPERATIONAL_PROJECTION_MISSING")
    else:
        reasons.extend(sorted(collection.hold_reasons))
        if collection.lifecycle != "active":
            reasons.append("collection_closed")
    # Customer scope survives new imports; review deadlines never release this block.
    scope_review = db.scalar(
        select(CustomerScopeReview.id)
        .where(
            CustomerScopeReview.tenant_id == invoice.tenant_id,
            CustomerScopeReview.provider_connection_id == invoice.provider_connection_id,
            CustomerScopeReview.customer_id == invoice.customer_id,
            CustomerScopeReview.released_at.is_(None),
        )
        .limit(1)
    )
    if scope_review is not None and "scope_review" not in reasons:
        reasons.append("scope_review")
    if pending_contact_stops(db, invoice) and "contact_stop_pending_review" not in reasons:
        reasons.append("contact_stop_pending_review")
    if (
        not invoice.verified
        or invoice.financial_status not in {"open", "partially_paid"}
        or invoice.balance_minor <= 0
    ):
        reasons.append("source_balance_ineligible")
    return GuardDecision(allowed=not reasons, reason=reasons[0] if reasons else None, reasons=reasons)


def contact_status(
    db: Session,
    tenant_id: UUID,
    customer_id: UUID,
    destination: str,
    at: datetime,
    *,
    cap_24h: int = 1,
    cap_7d: int = 3,
    exclude_operation_id: UUID | None = None,
) -> GuardDecision:
    rows = list(
        db.scalars(
            select(ContactReservation)
            .where(
                ContactReservation.tenant_id == tenant_id,
                or_(
                    ContactReservation.customer_id == customer_id,
                    ContactReservation.destination == destination,
                ),
                ContactReservation.status != "released",
                ContactReservation.operation_id != exclude_operation_id if exclude_operation_id else true(),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )

    def counts(days: int, selected: list[ContactReservation]) -> int:
        return sum(at - timedelta(days=days) < (r.submitted_at or r.reserved_at) <= at for r in selected)

    customer_rows = [r for r in rows if r.customer_id == customer_id]
    recipient_rows = [r for r in rows if r.destination == destination]
    count24, count7 = counts(1, customer_rows), counts(7, customer_rows)
    reason = "customer_contact_cap" if count24 >= min(1, cap_24h) or count7 >= min(3, cap_7d) else None
    if reason is None and (
        counts(1, recipient_rows) >= min(1, cap_24h) or counts(7, recipient_rows) >= min(3, cap_7d)
    ):
        reason = "recipient_contact_cap"
    return GuardDecision(
        allowed=reason is None,
        reason=reason,
        reasons=[reason] if reason else [],
        count_24h=count24,
        count_7d=count7,
    )
