"""Current policy/contact queries after the caller's sealed Voice grant/call authorization.

No identity context is constructed or promoted. Returned material contributes to the
independent approval binding; dynamic capacity is checked without changing that snapshot.
"""

from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.features.accounting.models import CustomerContact, InvoiceSnapshot
from aco.features.identity.models import Tenant
from aco.features.policies import checks
from aco.features.policies.dto import PolicyDefinition
from aco.features.policies.models import CollectionPolicyVersion, RecipientControl
from aco.features.policies.schedule import recipient_calendar
from aco.features.review.approvals import digest, stamp
from aco.features.voice.live import VoiceLiveGrant, config_for


def voice_policy_snapshot(
    db: Session,
    grant: VoiceLiveGrant,
    invoice_ids: list[UUID],
    at: datetime,
    *,
    exclude_operation_id: UUID | None = None,
    ongoing_session_id: UUID | None = None,
) -> tuple[dict[str, object], list[str]]:
    config = config_for(grant)
    if (
        not invoice_ids
        or len(set(invoice_ids)) != len(invoice_ids)
        or not set(invoice_ids) <= set(config.invoice_ids)
    ):
        raise HTTPException(403, "VOICE_LIVE_SCOPE_DENIED")
    tenant = db.get(Tenant, grant.tenant_id, populate_existing=True)
    version = db.get(CollectionPolicyVersion, grant.policy_version_id, populate_existing=True)
    if tenant is None or version is None or version.tenant_id != tenant.id:
        raise HTTPException(403, "VOICE_LIVE_POLICY_REQUIRED")
    definition = PolicyDefinition.model_validate(version.definition)
    reasons: list[str] = []
    if not definition.voice_enabled:
        reasons.append("voice_disabled")
    calendar = recipient_calendar(
        at,
        config.participant_timezone,
        opens_at=definition.opens_at,
        closes_at=definition.closes_at,
        weekdays=tuple(definition.weekdays),
    )
    if not calendar.allowed:
        reasons.append(calendar.reason or "quiet_hours")
    selected: list[dict[str, object]] = []
    customers = set()
    for key in invoice_ids:
        invoice = db.get(InvoiceSnapshot, key, populate_existing=True)
        if invoice is None or invoice.tenant_id != tenant.id:
            raise HTTPException(403, "VOICE_LIVE_SCOPE_DENIED")
        customers.add((invoice.provider_connection_id, invoice.customer_id))
        decision = checks.operational_status(db, invoice)
        reasons.extend(remaining_operational_reasons(db, invoice, ongoing_session_id, decision.reasons))
        if (
            at.astimezone(ZoneInfo(tenant.timezone)).date() - invoice.due_date
        ).days < definition.voice_after_days:
            reasons.append("voice_overdue_threshold")
        contact = db.scalar(
            select(CustomerContact)
            .where(
                CustomerContact.tenant_id == tenant.id,
                CustomerContact.provider_connection_id == invoice.provider_connection_id,
                CustomerContact.customer_id == invoice.customer_id,
            )
            .execution_options(populate_existing=True)
        )
        control = (
            db.scalar(
                select(RecipientControl)
                .where(RecipientControl.tenant_id == tenant.id, RecipientControl.contact_id == contact.id)
                .execution_options(populate_existing=True)
            )
            if contact
            else None
        )
        if (
            contact is None
            or control is None
            or not control.destination_verified
            or control.reviewed_destination != contact.email
            or control.reviewed_contact_revision != contact.source_revision
        ):
            reasons.append("recipient_unverified")
        if control and (control.opt_out or "voice" not in control.allowed_channels):
            reasons.append("recipient_channel_excluded")
        override = next((o for o in definition.overrides if o.customer_id == invoice.customer_id), None)
        if override and (override.opt_out or "voice" not in override.allowed_channels):
            reasons.append("customer_override_excluded")
        capacity = checks.contact_status(
            db,
            tenant.id,
            invoice.customer_id,
            config.participant_email,
            at,
            cap_24h=definition.cap_24h,
            cap_7d=definition.cap_7d,
            exclude_operation_id=exclude_operation_id,
        )
        reasons.extend(capacity.reasons)
        selected.append(
            {
                "invoice_id": str(invoice.id),
                "contact": stamp(contact, "email", "source_revision"),
                "control": stamp(
                    control,
                    "destination_verified",
                    "reviewed_destination",
                    "reviewed_contact_revision",
                    "allowed_channels",
                    "opt_out",
                ),
                "override": override.model_dump(mode="json") if override else None,
            }
        )
    if len(customers) != 1:
        raise HTTPException(403, "VOICE_LIVE_SCOPE_DENIED")
    material: dict[str, object] = {
        "grant_id": str(grant.id),
        "participant_id": str(grant.participant_id),
        "participant_email": config.participant_email,
        "participant_timezone": config.participant_timezone,
        "participant_approval_ref": config.participant_approval_ref,
        "capture": "DISABLED",
        "policy_version_id": str(version.id),
        "policy_version": version.version,
        "policy_hash": digest(version.definition),
        "selected": selected,
    }
    return material, sorted(set(reasons))


def remaining_operational_reasons(
    db: Session, invoice: InvoiceSnapshot, session_id: UUID | None, reasons: list[str]
) -> list[str]:
    """Only this already-active call may finish its own persisted case/promise facts.

    An independent hold, contact stop, closed balance or another call's restriction
    still denies tools. This is never used by create/join eligibility.
    """
    if session_id is None:
        return reasons
    from aco.features.cases.models import CaseEvidence, CaseInvoiceLink
    from aco.features.policies.models import CollectionHold
    from aco.features.promises.models import CustomerScopeReview, PaymentPromise, PromiseInvoiceLink
    from aco.features.voice.models import VoiceBusinessEvidence, VoiceSession

    session = db.get(VoiceSession, session_id)
    if (
        session is None
        or session.tenant_id != invoice.tenant_id
        or session.state not in {"active", "connected"}
    ):
        return reasons
    sources = list(
        db.scalars(
            select(VoiceBusinessEvidence).where(
                VoiceBusinessEvidence.tenant_id == invoice.tenant_id,
                VoiceBusinessEvidence.session_id == session_id,
            )
        )
    )
    ids = [source.id for source in sources]
    if not ids:
        return reasons
    permitted: set[UUID] = set()
    permitted.update(
        key
        for key in db.scalars(
            select(CaseInvoiceLink.hold_id)
            .join(CaseEvidence, CaseEvidence.case_id == CaseInvoiceLink.case_id)
            .where(
                CaseInvoiceLink.tenant_id == invoice.tenant_id,
                CaseEvidence.tenant_id == invoice.tenant_id,
                CaseInvoiceLink.invoice_id == invoice.id,
                CaseEvidence.voice_evidence_id.in_(ids),
            )
        )
        if key
    )
    promises = list(
        db.scalars(
            select(PaymentPromise).where(
                PaymentPromise.tenant_id == invoice.tenant_id,
                PaymentPromise.source_voice_evidence_id.in_(ids),
            )
        )
    )
    promise_ids = [row.id for row in promises]
    permitted.update(
        key
        for key in db.scalars(
            select(PromiseInvoiceLink.hold_id).where(
                PromiseInvoiceLink.tenant_id == invoice.tenant_id,
                PromiseInvoiceLink.invoice_id == invoice.id,
                PromiseInvoiceLink.promise_id.in_(promise_ids),
            )
        )
        if key
    )
    reviews = list(
        db.scalars(
            select(CustomerScopeReview).where(
                CustomerScopeReview.tenant_id == invoice.tenant_id,
                CustomerScopeReview.customer_id == invoice.customer_id,
                CustomerScopeReview.released_at.is_(None),
            )
        )
    )
    for review in reviews:
        if review.promise_id in promise_ids:
            permitted.update(UUID(key) for key in review.hold_ids)
    holds = list(
        db.scalars(
            select(CollectionHold).where(
                CollectionHold.tenant_id == invoice.tenant_id,
                CollectionHold.invoice_id == invoice.id,
                CollectionHold.released_at.is_(None),
            )
        )
    )
    # Every same-reason hold must be ours; projected-only restrictions remain denied.
    earliest = min(source.created_at for source in sources)
    own = {
        hold.id
        for hold in holds
        if hold.id in permitted and hold.creator_id == session.actor_id and hold.created_at >= earliest
    }
    allowed_reasons = {
        reason
        for reason in reasons
        if reason != "contact_stop_pending_review"
        and any(hold.reason == reason for hold in holds)
        and all(hold.id in own for hold in holds if hold.reason == reason)
    }
    if "scope_review" in allowed_reasons and any(review.promise_id not in promise_ids for review in reviews):
        allowed_reasons.remove("scope_review")
    return [reason for reason in reasons if reason not in allowed_reasons]
