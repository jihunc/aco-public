"""Current source references for explicit human issue disposition; no financial or contact effects."""

import hashlib
import json
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.features.accounting.models import CustomerContact, InvoiceSnapshot, VerifiedPaymentAllocation
from aco.features.actions.models import ScheduledAction
from aco.features.cases.dto import DispositionReference
from aco.features.identity.models import Actor, Membership, ProviderConnection
from aco.features.policies.contact_stop import contact_stop_source, pending_contact_stops
from aco.features.policies.models import RecipientControl


def material_hash(material: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def references(
    db: Session, invoice: InvoiceSnapshot, issue: str, resolver_id: UUID
) -> list[DispositionReference]:
    connection = db.get(ProviderConnection, invoice.provider_connection_id, populate_existing=True)
    if (
        connection is None
        or connection.tenant_id != invoice.tenant_id
        or connection.status != "READY"
        or connection.provider_type != "accounting"
        or not invoice.verified
    ):
        return []
    scope: dict[str, object] = {
        "tenant_id": str(invoice.tenant_id),
        "invoice_id": str(invoice.id),
        "customer_id": str(invoice.customer_id),
        "connection_id": str(connection.id),
        "connection_version": connection.version,
        "source_revision": invoice.source_revision,
        "currency": invoice.currency,
        "exponent": invoice.exponent,
    }
    result: list[DispositionReference] = []
    if issue == "refund":
        for refund in db.scalars(
            select(VerifiedPaymentAllocation)
            .where(
                VerifiedPaymentAllocation.tenant_id == invoice.tenant_id,
                VerifiedPaymentAllocation.provider_connection_id == connection.id,
                VerifiedPaymentAllocation.invoice_id == invoice.id,
                VerifiedPaymentAllocation.kind == "refund",
            )
            .order_by(VerifiedPaymentAllocation.id)
        ):
            parent = db.get(VerifiedPaymentAllocation, refund.parent_id) if refund.parent_id else None
            if (
                parent is None
                or parent.tenant_id != invoice.tenant_id
                or parent.provider_connection_id != connection.id
                or parent.invoice_id != invoice.id
                or parent.kind != "cash"
                or refund.minor_units >= 0
                or parent.minor_units < -refund.minor_units
                or (refund.currency, refund.exponent) != (invoice.currency, invoice.exponent)
                or (parent.currency, parent.exponent) != (invoice.currency, invoice.exponent)
                or refund.organisation_id != invoice.organisation_id
                or parent.organisation_id != invoice.organisation_id
            ):
                continue
            details: dict[str, object] = {
                "allocation_id": str(refund.id),
                "native_payment_id": refund.external_payment_id,
                "native_allocation_id": refund.external_allocation_id,
                "source_revision": refund.source_revision,
                "parent_allocation_id": str(parent.id),
                "parent_source_revision": parent.source_revision,
                "amount_minor": str(refund.minor_units),
                "currency": refund.currency,
                "exponent": refund.exponent,
                "effective_at": refund.effective_at.isoformat(),
                "observed_at": refund.observed_at.isoformat(),
            }
            result.append(
                DispositionReference(
                    id=refund.id,
                    version=refund.version,
                    resolution_kind="source_refund",
                    source_type="verified_refund",
                    label="Verified accounting refund",
                    synthetic=invoice.synthetic,
                    material_hash=material_hash({**scope, **details, "version": refund.version}),
                    details=details,
                )
            )
    elif issue == "stop_contact":
        for pending in pending_contact_stops(db, invoice):
            if pending["evidence_complete"] is not True:
                continue
            action = db.get(ScheduledAction, UUID(str(pending["action_id"])), populate_existing=True)
            if action is None:
                continue
            source = contact_stop_source(db, action)
            assert source is not None
            details = {
                "action_id": str(action.id),
                "holds": pending["holds"],
                "restriction_retained": True,
                "source_communication_id": str(source[1]) if source[1] else None,
                "source_voice_evidence_id": str(source[2]) if source[2] else None,
            }
            result.append(
                DispositionReference(
                    id=action.id,
                    version=action.version,
                    resolution_kind="verified_contact_restriction",
                    source_type="enforced_contact_stop",
                    label="Enforced customer contact restriction",
                    synthetic=invoice.synthetic,
                    material_hash=material_hash({**scope, **details, "version": action.version}),
                    details=details,
                )
            )
    if issue in {"contact_correction", "stop_contact"}:
        contact = db.scalar(
            select(CustomerContact)
            .where(
                CustomerContact.tenant_id == invoice.tenant_id,
                CustomerContact.provider_connection_id == connection.id,
                CustomerContact.customer_id == invoice.customer_id,
            )
            .execution_options(populate_existing=True)
        )
        control = (
            db.scalar(
                select(RecipientControl)
                .where(
                    RecipientControl.tenant_id == invoice.tenant_id,
                    RecipientControl.provider_connection_id == connection.id,
                    RecipientControl.contact_id == contact.id,
                )
                .execution_options(populate_existing=True)
            )
            if contact
            else None
        )
        reviewer = db.get(Actor, control.reviewer_id, populate_existing=True) if control else None
        membership = (
            db.scalar(
                select(Membership)
                .where(
                    Membership.tenant_id == invoice.tenant_id,
                    Membership.user_id == control.reviewer_id,
                )
                .execution_options(populate_existing=True)
            )
            if control
            else None
        )
        if (
            contact is not None
            and control is not None
            and reviewer is not None
            and reviewer.kind == "human"
            and reviewer.active
            and membership is not None
            and membership.active
            and membership.role == "owner"
            and contact.email
            and control.destination_verified
            and control.evidence.strip()
            and control.reviewed_destination == contact.email
            and control.reviewed_contact_revision == contact.source_revision
            and (issue != "contact_correction" or control.reviewer_id != resolver_id)
            and (issue != "stop_contact" or control.opt_out)
        ):
            details = {
                "contact_id": str(contact.id),
                "destination": contact.email,
                "contact_source_revision": contact.source_revision,
                "reviewer_id": str(control.reviewer_id),
                "reviewer_membership_version": membership.version,
                "reviewer_actor_version": reviewer.version,
                "review_evidence": control.evidence,
                "opt_out_retained": control.opt_out,
                "allowed_channels": list(control.allowed_channels),
                "contact_authority_changed": False,
            }
            result.append(
                DispositionReference(
                    id=control.id,
                    version=control.version,
                    resolution_kind="verified_contact_correction"
                    if issue == "contact_correction"
                    else "verified_contact_restriction",
                    source_type="verified_contact_review"
                    if issue == "contact_correction"
                    else "verified_opt_out",
                    label="Independent Owner-reviewed current contact"
                    if issue == "contact_correction"
                    else "Current Owner-reviewed opt-out",
                    synthetic=invoice.synthetic,
                    material_hash=material_hash({**scope, **details, "version": control.version}),
                    details=details,
                )
            )
    return result


def verify_reference(
    db: Session,
    invoice: InvoiceSnapshot,
    issue: str,
    *,
    reference_id: UUID | None,
    resolver_id: UUID,
    kind: str,
    version: int | None,
    captured_hash: str | None,
) -> DispositionReference:
    if version is None or captured_hash is None:
        raise HTTPException(422, "CAPTURED_DISPOSITION_REFERENCE_REQUIRED")
    reference = next((r for r in references(db, invoice, issue, resolver_id) if r.id == reference_id), None)
    if reference is None or reference.resolution_kind != kind:
        raise HTTPException(422, "VERIFIED_DISPOSITION_REFERENCE_REQUIRED")
    if reference.version != version or reference.material_hash != captured_hash:
        raise HTTPException(409, "DISPOSITION_REFERENCE_CHANGED")
    return reference
