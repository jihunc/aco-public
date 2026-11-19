"""Source-backed commitment performance; financial status never substitutes for allocation evidence."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.clock import Clock, SystemClock
from aco.core.context import ActorContext, ServiceContext
from aco.core.types import Money, ProviderErrorCode
from aco.features.accounting.dto import AccountingResult, InvoiceData, PaymentData
from aco.features.accounting.models import AccountingObservation, InvoiceSnapshot, VerifiedPaymentAllocation
from aco.features.accounting.reconcile import SourceReadContext
from aco.features.actions.models import ScheduledAction
from aco.features.cases.service import create_review_task, place_hold, release_hold
from aco.features.identity.models import ProviderConnection
from aco.features.identity.service import IdentityService, Permission
from aco.features.promises.dto import PromiseAllocation
from aco.features.promises.models import (
    PaymentPromise,
    PromiseAllocationClaim,
    PromiseInstallment,
    PromiseVerification,
)
from aco.features.promises.service import PromiseService
from aco.integrations.ports.accounting import AccountingRejectionEvidence


@dataclass
class SourceEvidence:
    observation: AccountingObservation | None
    allocations: list[VerifiedPaymentAllocation]
    reason: str | None


def current_source(db: Session, tenant: UUID, invoice_id: UUID, at: datetime) -> SourceEvidence:
    invoice = db.get(InvoiceSnapshot, invoice_id, populate_existing=True)
    if invoice is None or invoice.tenant_id != tenant:
        raise HTTPException(403, "PROMISE_SOURCE_SCOPE_MISMATCH")
    observation = db.scalar(
        select(AccountingObservation)
        .where(
            AccountingObservation.tenant_id == tenant,
            AccountingObservation.invoice_id == invoice_id,
        )
        .order_by(
            AccountingObservation.observed_at.desc(),
            AccountingObservation.created_at.desc(),
            AccountingObservation.id.desc(),
        )
    )
    allocations = list(
        db.scalars(
            select(VerifiedPaymentAllocation)
            .where(
                VerifiedPaymentAllocation.tenant_id == tenant,
                VerifiedPaymentAllocation.invoice_id == invoice_id,
                VerifiedPaymentAllocation.provider_connection_id == invoice.provider_connection_id,
            )
            .order_by(VerifiedPaymentAllocation.effective_at, VerifiedPaymentAllocation.id)
        )
    )
    if observation is None:
        return SourceEvidence(None, allocations, "source_unavailable")
    try:
        invoice_provenance = observation.provenance["invoice"]
        payment_provenance = observation.provenance["payments"]
        if not isinstance(invoice_provenance, dict) or not isinstance(payment_provenance, dict):
            return SourceEvidence(observation, allocations, "source_evidence_unreadable")
        observed = InvoiceData.model_validate_json(json.dumps(observation.invoice_data))
        inv_result = AccountingResult[InvoiceData].model_validate_json(
            json.dumps(
                {
                    **invoice_provenance,
                    "value": observation.invoice_data,
                }
            )
        )
        pay_result = AccountingResult[list[PaymentData]].model_validate_json(
            json.dumps(
                {
                    **payment_provenance,
                    "value": observation.payment_data,
                }
            )
        )
        connection = db.get(ProviderConnection, invoice.provider_connection_id, populate_existing=True)
        if connection is None or connection.status != "READY":
            return SourceEvidence(observation, allocations, "source_connection_unavailable")
        for result in (inv_result, pay_result):
            if (
                result.tenant_id != tenant
                or result.provider_connection_id != invoice.provider_connection_id
                or result.organisation_id != invoice.organisation_id
                or result.provider != connection.provider
                or result.authenticity != "VERIFIED"
                or result.currentness != "CURRENT"
                or not timedelta(0) <= at - result.observed_at <= timedelta(minutes=15)
            ):
                return SourceEvidence(observation, allocations, "source_stale_or_unverified")
        if (
            not invoice.verified
            or not observed.verified
            or observed.external_id != invoice.external_invoice_id
            or observed.stamp.revision != invoice.source_revision
            or observed.stamp.connection_id != invoice.provider_connection_id
            or invoice.last_read_at != observation.observed_at
            or pay_result.next_cursor is not None
            or not timedelta(0) <= at - observed.stamp.observed_at <= timedelta(minutes=15)
        ):
            return SourceEvidence(observation, allocations, "source_incomplete_or_changed")
        persisted = {(a.external_payment_id, a.external_allocation_id): a for a in allocations}
        if set(persisted) != {(p.external_payment_id, p.external_allocation_id) for p in pay_result.value}:
            return SourceEvidence(observation, allocations, "allocation_history_incomplete")
        for payment in pay_result.value:
            allocation = persisted[(payment.external_payment_id, payment.external_allocation_id)]
            parent = db.get(VerifiedPaymentAllocation, allocation.parent_id) if allocation.parent_id else None
            if (
                not payment.verified
                or payment.stamp.connection_id != invoice.provider_connection_id
                or payment.external_invoice_id != invoice.external_invoice_id
                or payment.organisation_id != invoice.organisation_id
                or (payment.amount.currency, payment.amount.exponent) != (invoice.currency, invoice.exponent)
                or (
                    allocation.kind,
                    allocation.minor_units,
                    allocation.currency,
                    allocation.exponent,
                    allocation.effective_at,
                    allocation.source_revision,
                )
                != (
                    payment.kind,
                    payment.amount.minor_units,
                    payment.amount.currency,
                    payment.amount.exponent,
                    payment.stamp.source_at,
                    payment.stamp.revision,
                )
                or (payment.parent_payment_id, payment.parent_allocation_id)
                != ((parent.external_payment_id, parent.external_allocation_id) if parent else (None, None))
            ):
                return SourceEvidence(observation, allocations, "allocation_evidence_ambiguous")
    except (ValidationError, KeyError, TypeError):
        return SourceEvidence(observation, allocations, "source_evidence_unreadable")
    return SourceEvidence(observation, allocations, None)


def verify_invoice_promises(
    db: Session,
    ctx: ActorContext,
    observation_id: UUID,
    *,
    clock: Clock | None = None,
) -> list[UUID]:
    """Reconcile only commitments linked to an actual current source observation under fresh authority."""
    if isinstance(ctx, SourceReadContext):
        IdentityService(db).authorize(ctx, Permission.INVOICE_READ)
        if ctx.observation_id != observation_id:
            raise HTTPException(403, "PROMISE_OBSERVATION_OUTSIDE_CONTINUATION")
    else:
        IdentityService(db).authorize(ctx, Permission.DEMO_PAYMENT)
    observation = db.get(AccountingObservation, observation_id, populate_existing=True)
    if observation is None or observation.tenant_id != ctx.tenant_id:
        raise HTTPException(403, "PROMISE_OBSERVATION_SCOPE_MISMATCH")
    invoice = db.get(InvoiceSnapshot, observation.invoice_id, populate_existing=True)
    if invoice is None or invoice.provider_connection_id != observation.provider_connection_id:
        raise HTTPException(403, "PROMISE_OBSERVATION_SCOPE_MISMATCH")
    bound_promise: UUID | None = None
    if isinstance(ctx, ServiceContext):
        action = db.get(ScheduledAction, ctx.action_id, populate_existing=True)
        if (
            action is None
            or action.invoice_id != invoice.id
            or action.provider_connection_id != observation.provider_connection_id
        ):
            raise HTTPException(403, "PROMISE_OBSERVATION_OUTSIDE_ACTION")
        if action.kind == "promise_check":
            from aco.features.promises.scheduling import validate_promise_check

            bound_promise = validate_promise_check(db, action).id
    return _verify_promises(db, ctx, invoice.id, bound_promise, (clock or SystemClock()).now())


def record_promise_source_failure(
    db: Session,
    ctx: ActorContext,
    invoice_id: UUID,
    reason: str,
    *,
    clock: Clock | None = None,
    rejection_evidence: AccountingRejectionEvidence | None = None,
) -> list[UUID]:
    """Record a real failed read only while its exact promise worker attempt still owns authority."""
    if not isinstance(ctx, ServiceContext) or isinstance(ctx, SourceReadContext):
        raise HTTPException(403, "PROMISE_FAILURE_REQUIRES_EXECUTING_WORKER")
    IdentityService(db).authorize(ctx, Permission.DEMO_PAYMENT)
    action = db.get(ScheduledAction, ctx.action_id, populate_existing=True)
    if (
        action is None
        or action.tenant_id != ctx.tenant_id
        or action.kind != "promise_check"
        or action.status != "executing"
        or action.invoice_id != invoice_id
    ):
        raise HTTPException(403, "PROMISE_FAILURE_OUTSIDE_ACTION")
    from aco.features.promises.scheduling import validate_promise_check

    promise = validate_promise_check(db, action)
    if rejection_evidence is not None:
        rejection_evidence = AccountingRejectionEvidence.model_validate(rejection_evidence.model_dump())
        invoice = db.get(InvoiceSnapshot, invoice_id, populate_existing=True)
        if (
            invoice is None
            or invoice.tenant_id != ctx.tenant_id
            or rejection_evidence.tenant_id != ctx.tenant_id
            or rejection_evidence.provider_connection_id != action.provider_connection_id
            or invoice.provider_connection_id != action.provider_connection_id
            or rejection_evidence.external_invoice_id != invoice.external_invoice_id
        ):
            raise HTTPException(403, "PROMISE_REJECTED_SOURCE_OUTSIDE_ACTION")
    try:
        code = ProviderErrorCode(reason).value
    except ValueError as exc:
        raise HTTPException(422, "PROMISE_FAILURE_CODE_INVALID") from exc
    return _verify_promises(
        db,
        ctx,
        invoice_id,
        promise.id,
        (clock or SystemClock()).now(),
        failure_reason=code,
        rejection_evidence=rejection_evidence,
    )


def _verify_promises(
    db: Session,
    ctx: ActorContext,
    invoice_id: UUID,
    bound_promise: UUID | None,
    at: datetime,
    *,
    failure_reason: str | None = None,
    rejection_evidence: AccountingRejectionEvidence | None = None,
) -> list[UUID]:
    from aco.features.promises.models import PromiseInvoiceLink

    promises = list(
        db.scalars(
            select(PaymentPromise)
            .where(
                PaymentPromise.tenant_id == ctx.tenant_id,
                PaymentPromise.status.in_(["ACTIVE", "FULFILLED", "MISSED"]),
                PaymentPromise.id.in_(
                    select(PromiseInvoiceLink.promise_id).where(
                        PromiseInvoiceLink.tenant_id == ctx.tenant_id,
                        PromiseInvoiceLink.invoice_id == invoice_id,
                    )
                ),
            )
            .order_by(PaymentPromise.confirmed_at, PaymentPromise.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    changed = []
    for promise in promises:
        service = PromiseService(db)
        links = service._links(promise)
        # Existing action authority is one invoice; broader checks require the exact promise trigger.
        if bound_promise is not None and promise.id != bound_promise:
            continue
        if (
            isinstance(ctx, ServiceContext)
            and bound_promise is None
            and {link.invoice_id for link in links} != {invoice_id}
        ):
            continue
        if promise.confirmed_at is None:
            raise HTTPException(409, "PROMISE_CONFIRMATION_UNAVAILABLE")
        sources = {link.invoice_id: current_source(db, ctx.tenant_id, link.invoice_id, at) for link in links}
        failure_context: dict[str, object] | None = None
        if failure_reason is not None:
            assert isinstance(ctx, ServiceContext)
            sources[invoice_id].reason = f"provider_failure:{failure_reason}"
            failure_context = {
                "code": failure_reason,
                "action_id": str(ctx.action_id),
                "action_version": ctx.action_version,
                "job_id": ctx.job_id,
                "lease_token": str(ctx.lease_token),
            }
            if rejection_evidence is not None:
                failure_context["rejection_evidence"] = rejection_evidence.model_dump(mode="json")
        for installment in service._installments(promise):
            if _verify_installment(db, ctx, promise, installment, sources, at, failure_context):
                changed.append(promise.id)
        installments = service._installments(promise)
        statuses = {i.status for i in installments}
        target = "FULFILLED" if statuses == {"FULFILLED"} else "MISSED" if "MISSED" in statuses else "ACTIVE"
        results = [
            db.scalar(
                select(PromiseVerification)
                .where(
                    PromiseVerification.tenant_id == ctx.tenant_id,
                    PromiseVerification.installment_id == item.id,
                )
                .order_by(PromiseVerification.created_at.desc(), PromiseVerification.id.desc())
            )
            for item in installments
        ]
        pending = not results or any(
            result is None or result.verification_status != "VERIFIED" for result in results
        )
        if not pending and promise.status != target:
            old = promise.status
            promise.status = target
            promise.version += 1
            service._event(
                ctx, promise, "promise.performance_status", {"prior_status": old, "status": target}
            )
        for link in links:
            if pending:
                if link.verification_hold_id is None:
                    link.verification_hold_id = place_hold(
                        db,
                        ctx,
                        link.invoice_id,
                        "promise_verification",
                        f"Pending source verification for promise {promise.id}",
                    )
            else:
                release_hold(db, ctx, link.verification_hold_id, f"Verified source for promise {promise.id}")
                link.verification_hold_id = None
                active = any(
                    i.status == "ACTIVE"
                    and any(str(a["invoice_id"]) == str(link.invoice_id) for a in i.allocations)
                    for i in installments
                )
                if active:
                    from aco.features.policies.models import CollectionHold

                    hold = db.get(CollectionHold, link.hold_id) if link.hold_id else None
                    if hold is None or hold.released_at is not None:
                        link.hold_id = place_hold(
                            db,
                            ctx,
                            link.invoice_id,
                            "active_promise",
                            f"Verified insufficient performance before cutoff for {promise.id}",
                        )
                else:
                    release_hold(
                        db, ctx, link.hold_id, f"Verified promise performance {promise.id}: {promise.status}"
                    )
        db.flush()
    return list(dict.fromkeys(changed))


def _verify_installment(
    db: Session,
    ctx: ActorContext,
    promise: PaymentPromise,
    installment: PromiseInstallment,
    sources: dict[UUID, SourceEvidence],
    at: datetime,
    failure_context: dict[str, object] | None = None,
) -> bool:
    amount = Money.model_validate(installment.amount)
    parts = [PromiseAllocation.model_validate_json(json.dumps(a)) for a in installment.allocations]
    reason = next((source.reason for source in sources.values() if source.reason), None)
    if not parts or sum(p.amount.minor_units for p in parts) != amount.minor_units:
        reason = "installment_scope_unresolved"
    timely = late = outside = 0
    excluded = []
    counted = []
    unclaimed: list[tuple[VerifiedPaymentAllocation, int]] = []
    reversals: list[str] = []
    all_met = True
    all_eventual_met = True
    assert promise.confirmed_at is not None
    all_installments = PromiseService(db)._installments(promise)
    order = {item.id: index for index, item in enumerate(all_installments)}
    for part in parts:
        if part.invoice_id not in sources or (part.amount.currency, part.amount.exponent) != (
            amount.currency,
            amount.exponent,
        ):
            reason = "installment_scope_unresolved"
            continue
        source = sources[part.invoice_id]
        net = 0
        part_late = 0
        for cash in source.allocations:
            if cash.kind != "cash":
                continue
            if (cash.currency, cash.exponent) != (amount.currency, amount.exponent):
                reason = "allocation_currency_ambiguous"
                continue
            claims = list(
                db.scalars(
                    select(PromiseAllocationClaim).where(
                        PromiseAllocationClaim.tenant_id == ctx.tenant_id,
                        PromiseAllocationClaim.allocation_id == cash.id,
                    )
                )
            )
            if any(claim.promise_id != promise.id for claim in claims):
                excluded.append(str(cash.id))
                continue
            claim = next((item for item in claims if item.installment_id == installment.id), None)
            earlier_claimed = sum(
                item.claimed_minor for item in claims if order[item.installment_id] < order[installment.id]
            )
            remaining_gross = cash.minor_units - sum(item.claimed_minor for item in claims)
            refunds = [a for a in source.allocations if a.parent_id == cash.id and a.effective_at <= at]
            reversals.extend(str(a.id) for a in refunds)
            residual = cash.minor_units + sum(a.minor_units for a in refunds)
            if residual < 0:
                reason = "reversal_exceeds_source_cash"
                continue
            if cash.effective_at > at:
                continue
            if cash.effective_at < promise.confirmed_at:
                outside += residual
            else:
                if claim is None and net + part_late >= part.amount.minor_units:
                    # Preserve existing ownership, but do not seize later independent
                    # cash once this installment is already covered.
                    excluded.append(str(cash.id))
                    continue
                last_for_invoice = not any(
                    order[item.id] > order[installment.id]
                    and any(str(a["invoice_id"]) == str(part.invoice_id) for a in item.allocations)
                    for item in all_installments
                )
                portion = (
                    claim.claimed_minor
                    if claim
                    else (
                        remaining_gross
                        if last_for_invoice
                        else min(remaining_gross, max(0, part.amount.minor_units - net - part_late))
                    )
                )
                contribution = min(portion, max(0, residual - earlier_claimed))
                if cash.effective_at >= installment.cutoff_at:
                    late += contribution
                    part_late += contribution
                else:
                    net += contribution
                if portion:
                    counted.append(str(cash.id))
                    if claim is None:
                        unclaimed.append((cash, portion))
        timely += net
        all_met = all_met and net >= part.amount.minor_units
        all_eventual_met = all_eventual_met and net + part_late >= part.amount.minor_units
    details: dict[str, object] = {
        "observation_ids": [str(s.observation.id) for s in sources.values() if s.observation],
        "allocation_ids": counted,
        "excluded_allocation_ids": excluded,
        "reversal_ids": reversals,
        "reason": reason,
        "cutoff_elapsed": at >= installment.cutoff_at,
        "timely_minor": str(timely) if reason is None else None,
        "late_minor": str(late) if reason is None else None,
        "outside_minor": str(outside) if reason is None else None,
        "performance": None
        if reason
        else "LATE_FULFILLED"
        if not all_met and all_eventual_met
        else "OUTSIDE_WINDOW"
        if outside and not timely
        else "ON_TIME",
    }
    if failure_context is not None:
        details["source_failure"] = failure_context
    key = hashlib.sha256(json.dumps(details, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    previous = db.scalar(
        select(PromiseVerification).where(
            PromiseVerification.tenant_id == ctx.tenant_id,
            PromiseVerification.installment_id == installment.id,
            PromiseVerification.evidence_key == key,
        )
    )
    if previous is not None:
        if previous.details != details:
            raise HTTPException(409, "PROMISE_VERIFICATION_IDENTITY_COLLISION")
        return False
    status = (
        installment.status
        if reason
        else "FULFILLED"
        if all_met
        else "MISSED"
        if at >= installment.cutoff_at
        else "ACTIVE"
    )
    svc = PromiseService(db)
    latest = db.scalar(
        select(PromiseVerification)
        .where(
            PromiseVerification.tenant_id == ctx.tenant_id,
            PromiseVerification.installment_id == installment.id,
        )
        .order_by(PromiseVerification.created_at.desc(), PromiseVerification.id.desc())
    )
    if reason is None:
        for cash, portion in unclaimed:
            db.add(
                PromiseAllocationClaim(
                    tenant_id=ctx.tenant_id,
                    provider_connection_id=cash.provider_connection_id,
                    allocation_id=cash.id,
                    claimed_minor=portion,
                    promise_id=promise.id,
                    installment_id=installment.id,
                )
            )
    prior = installment.status
    event = svc._event(
        ctx,
        promise,
        "promise.verification_pending" if reason else "promise.performance_verified",
        {
            **details,
            "installment_id": str(installment.id),
            "prior_status": prior,
            "status": status,
        },
    )
    db.add(
        PromiseVerification(
            tenant_id=ctx.tenant_id,
            promise_id=promise.id,
            installment_id=installment.id,
            event_id=event.id,
            evidence_key=key,
            verification_status="PENDING_REVIEW" if reason else "VERIFIED",
            prior_status=prior,
            resulting_status=status,
            timely_minor=None if reason else timely,
            late_minor=None if reason else late,
            outside_minor=None if reason else outside,
            checked_at=at,
            details=details,
        )
    )
    if reason:
        if latest is None or latest.verification_status != "PENDING_REVIEW":
            promise.review_task_id = create_review_task(
                db,
                ctx,
                "Verify payment promise performance",
                f"Promise {promise.id}: {reason}; source observations {details['observation_ids']}",
                parts[0].invoice_id if parts else None,
                at + timedelta(hours=24),
            )
    else:
        installment.status = status
        installment.version += 1
        if prior != status:
            svc._event(
                ctx,
                promise,
                "promise.installment_status",
                {"installment_id": str(installment.id), "prior_status": prior, "status": status},
            )
    db.flush()
    return True
