"""Pure current projection queries, called only after ScopedReads validates the run.

These facts neither authorize contact nor reverify accounting for dispatch. They
retain unavailable policy, unreviewed recipients and uncertain exposure explicitly.
"""

from datetime import datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aco.features.accounting.models import CollectionRecord, CustomerContact, InvoiceSnapshot
from aco.features.identity.models import Tenant
from aco.features.policies.contact_stop import pending_contact_stops
from aco.features.policies.dto import PolicyDefinition
from aco.features.policies.guards import normalized_destination
from aco.features.policies.models import (
    CollectionHold,
    CollectionPolicy,
    CollectionPolicyVersion,
    ContactReservation,
    RecipientControl,
)
from aco.features.promises.models import CustomerScopeReview
from aco.features.tools.schemas import PolicyContactFact, PolicyFact, RecipientStatusFact, SourceStamp


def current_policy(db: Session, tenant_id: UUID) -> CollectionPolicyVersion | None:
    return db.scalar(
        select(CollectionPolicyVersion)
        .join(
            CollectionPolicy,
            CollectionPolicy.id == CollectionPolicyVersion.policy_id,
        )
        .where(
            CollectionPolicyVersion.tenant_id == tenant_id,
            CollectionPolicy.tenant_id == tenant_id,
            CollectionPolicy.active.is_(True),
            CollectionPolicy.archived_at.is_(None),
            CollectionPolicyVersion.state == "ACTIVE",
        )
        .order_by(CollectionPolicyVersion.created_at.desc(), CollectionPolicyVersion.id)
        .limit(1)
        .execution_options(populate_existing=True)
    )


def policy_fact(row: CollectionPolicyVersion) -> PolicyFact:
    return PolicyFact(
        policy_id=row.policy_id,
        version_id=row.id,
        version=row.version,
        state=row.state,
        definition=row.definition,
        updated_at=row.updated_at,
        synthetic=row.synthetic,
    )


def contact_snapshot(
    db: Session, invoice: InvoiceSnapshot, source: SourceStamp, at: datetime
) -> PolicyContactFact:
    tenant = db.get(Tenant, invoice.tenant_id, populate_existing=True)
    assert tenant is not None  # The sealed current_run boundary already proved this tenant.
    policy = current_policy(db, invoice.tenant_id)
    definition = PolicyDefinition.model_validate(policy.definition) if policy else None
    override = (
        next((o for o in definition.overrides if o.customer_id == invoice.customer_id), None)
        if definition
        else None
    )
    collection = db.scalar(
        select(CollectionRecord)
        .where(
            CollectionRecord.tenant_id == invoice.tenant_id,
            CollectionRecord.invoice_id == invoice.id,
        )
        .execution_options(populate_existing=True)
    )
    holds = list(
        db.scalars(
            select(CollectionHold.reason)
            .where(
                CollectionHold.tenant_id == invoice.tenant_id,
                CollectionHold.invoice_id == invoice.id,
                CollectionHold.released_at.is_(None),
            )
            .order_by(CollectionHold.id)
            .limit(51)
        )
    )
    hold_count = (
        db.scalar(
            select(func.count())
            .select_from(CollectionHold)
            .where(
                CollectionHold.tenant_id == invoice.tenant_id,
                CollectionHold.invoice_id == invoice.id,
                CollectionHold.released_at.is_(None),
            )
        )
        or 0
    )
    reviews = (
        db.scalar(
            select(func.count())
            .select_from(CustomerScopeReview)
            .where(
                CustomerScopeReview.tenant_id == invoice.tenant_id,
                CustomerScopeReview.provider_connection_id == invoice.provider_connection_id,
                CustomerScopeReview.customer_id == invoice.customer_id,
                CustomerScopeReview.released_at.is_(None),
            )
        )
        or 0
    )  # Deadline escalation is not release.
    pending_stops = len(pending_contact_stops(db, invoice))
    reasons = set(holds[:50]) | set(
        collection.hold_reasons if collection else ["OPERATIONAL_PROJECTION_MISSING"]
    )
    if reviews:
        reasons.add("scope_review")
    if pending_stops:
        reasons.add("contact_stop_pending_review")
    contact = db.scalar(
        select(CustomerContact)
        .where(
            CustomerContact.tenant_id == invoice.tenant_id,
            CustomerContact.customer_id == invoice.customer_id,
            CustomerContact.provider_connection_id == invoice.provider_connection_id,
        )
        .execution_options(populate_existing=True)
    )
    control = (
        db.scalar(
            select(RecipientControl)
            .where(
                RecipientControl.tenant_id == invoice.tenant_id,
                RecipientControl.provider_connection_id == invoice.provider_connection_id,
                RecipientControl.contact_id == contact.id,
            )
            .execution_options(populate_existing=True)
        )
        if contact
        else None
    )
    matches = bool(
        contact
        and control
        and control.reviewed_destination == contact.email
        and control.reviewed_contact_revision == contact.source_revision
    )
    recipient = (
        RecipientStatusFact(
            contact_id=contact.id,
            source_revision=contact.source_revision,
            control_id=control.id if control else None,
            control_version=control.version if control else None,
            destination_present=bool(contact.email),
            destination_verified=bool(control and control.destination_verified),
            review_matches_current_source=matches,
            opt_out=bool(control and control.opt_out),
            allowed_channels=control.allowed_channels if control else [],
            timezone=control.timezone if control else None,
            timezone_verified=bool(control and control.timezone_verified),
            owner_confirmed_tenant_timezone=bool(
                control
                and control.owner_confirmed_tenant_timezone
                and control.confirmed_tenant_timezone == tenant.timezone
            ),
        )
        if contact
        else None
    )
    # Use the canonical normalization/count window; expose counts, never other customers' destinations.
    destination = None
    if contact and contact.email:
        try:
            destination = normalized_destination(contact.email)
        except HTTPException:
            pass  # Invalid current destination stays visibly unavailable.
    timestamp = func.coalesce(ContactReservation.submitted_at, ContactReservation.reserved_at)
    counts = db.execute(
        select(
            func.count().filter(
                ContactReservation.customer_id == invoice.customer_id, timestamp > at - timedelta(days=1)
            ),
            func.count().filter(ContactReservation.customer_id == invoice.customer_id),
            func.count().filter(
                ContactReservation.destination == destination, timestamp > at - timedelta(days=1)
            ),
            func.count().filter(ContactReservation.destination == destination),
            func.count().filter(
                ContactReservation.customer_id == invoice.customer_id,
                ContactReservation.status == "unknown_outcome",
            ),
        ).where(
            ContactReservation.tenant_id == invoice.tenant_id,
            ContactReservation.status != "released",
            timestamp > at - timedelta(days=7),
            timestamp <= at,
        )
    ).one()
    return PolicyContactFact(
        invoice_id=invoice.id,
        customer_id=invoice.customer_id,
        provider_connection_id=invoice.provider_connection_id,
        observed_at=at,
        policy=policy_fact(policy) if policy else None,
        policy_state="ACTIVE" if policy else "UNAVAILABLE",
        collection_lifecycle=collection.lifecycle if collection else None,
        hold_reasons=sorted(reasons)[:50],
        active_hold_count=hold_count,
        hold_details_truncated=hold_count > 50 or len(reasons) > 50,
        unresolved_customer_scope_reviews=reviews,
        pending_contact_stops=pending_stops,
        recipient=recipient,
        customer_override_opt_out=override.opt_out if override else None,
        customer_override_allowed_channels=list(override.allowed_channels) if override else None,
        verified_email_recipient_available=bool(
            destination
            and recipient
            and matches
            and recipient.destination_verified
            and not recipient.opt_out
            and "email" in recipient.allowed_channels
            and (override is None or not override.opt_out and "email" in override.allowed_channels)
        ),
        customer_exposure_24h=counts[0],
        customer_exposure_7d=counts[1],
        recipient_exposure_24h=counts[2],
        recipient_exposure_7d=counts[3],
        unknown_exposure_7d=counts[4],
        cap_24h=definition.cap_24h if definition else None,
        cap_7d=definition.cap_7d if definition else None,
        outbound_paused=tenant.outbound_paused,
        automation_enabled=tenant.automation_enabled,
        source=source,
        snapshot_observed_within_15m=timedelta(0) <= at - invoice.observed_at <= timedelta(minutes=15),
    )
