"""Validate complete adapter history against the actual canonical imported cash identities."""

import json
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from aco.features.accounting.dto import InvoiceData
from aco.features.accounting.models import AccountingObservation, InvoiceSnapshot, VerifiedPaymentAllocation
from aco.features.impact.cohorts import AllocationFact, CohortHistory, horizon
from aco.integrations.ports.accounting_history import CohortSourceHistory, HistoricalInvoice


def materialize(
    history: CohortSourceHistory,
    source: HistoricalInvoice,
    invoice: InvoiceSnapshot,
    allocations: list[VerifiedPaymentAllocation],
    *,
    frozen: CohortHistory | None = None,
    historical_observation: AccountingObservation | None = None,
) -> CohortHistory:
    start, end = horizon(
        source.original_due_date if frozen is None else frozen.original_due_date, history.timezone
    )
    as_of = history.complete_through
    revisions = sorted(
        source.revisions, key=lambda row: (row.effective_at, int(row.invoice.stamp.revision or "0"))
    )
    before_d = [row for row in revisions if row.effective_at < start and row.observed_at <= as_of]
    before_h = [row for row in revisions if row.effective_at < end and row.observed_at <= as_of]
    current = revisions[-1] if revisions else None
    first = revisions[0] if revisions else None
    native = {(row.external_payment_id, row.external_allocation_id): row for row in allocations}
    facts: list[AllocationFact] = []
    complete = bool(source.history_complete and first and current and before_d and before_h)
    consistent = complete
    if first is not None:
        issue = datetime.combine(source.issue_date, time.min, ZoneInfo(history.timezone)).astimezone(UTC)
        consistent = (
            consistent and first.effective_at == issue and first.invoice.outstanding == first.invoice.original
        )
        consistent = consistent and sorted(
            int(row.invoice.stamp.revision or "0") for row in revisions
        ) == list(range(1, len(revisions) + 1))
        consistent = (
            consistent
            and first.invoice.due_date == source.original_due_date
            and first.invoice.issue_date == source.issue_date
        )
    seen: set[tuple[str, str]] = set()
    for row in source.allocations:
        payment = row.payment
        key = (payment.external_payment_id, payment.external_allocation_id)
        imported = native.get(key)
        if imported is None or key in seen:
            complete = False
            continue
        seen.add(key)
        valid = (
            imported.tenant_id == invoice.tenant_id
            and imported.provider_connection_id == invoice.provider_connection_id
            and imported.invoice_id == invoice.id
            and imported.organisation_id == invoice.organisation_id
            and imported.synthetic == history.synthetic
            and payment.verified
            and payment.stamp.synthetic == history.synthetic
            and imported.currency == payment.amount.currency == invoice.currency
            and imported.exponent == payment.amount.exponent == invoice.exponent
            and imported.minor_units == payment.amount.minor_units
            and imported.kind == payment.kind
            and imported.effective_at == row.effective_at
            and imported.source_revision == payment.stamp.revision
            and imported.created_at <= as_of
            and imported.observed_at <= as_of
        )
        parent = native.get((payment.parent_payment_id or "", payment.parent_allocation_id or ""))
        valid = valid and imported.parent_id == (parent.id if parent else None)
        consistent = consistent and valid
        facts.append(
            AllocationFact(
                id=str(imported.id),
                kind=payment.kind,
                minor_units=payment.amount.minor_units,
                parent_id=str(parent.id) if parent else None,
                effective_at=row.effective_at,
                observed_at=row.observed_at,
            )
        )
    complete = complete and len(seen) == len(native)
    for revision in revisions:
        value = revision.invoice
        valid = (
            value.verified
            and value.stamp.synthetic == history.synthetic
            and value.external_id == invoice.external_invoice_id
            and value.organisation_id == invoice.organisation_id
            and value.stamp.connection_id == invoice.provider_connection_id
            and value.original.currency == invoice.currency
            and value.original.exponent == invoice.exponent
            and value.original.minor_units
            == (first.invoice.original.minor_units if first else invoice.original_minor)
            and value.issue_date == source.issue_date
        )
        balance = value.original.minor_units - sum(
            item.minor_units
            for item in facts
            if item.effective_at <= revision.effective_at and item.observed_at <= revision.observed_at
        )
        valid = valid and (
            value.outstanding.minor_units == balance or value.outstanding.minor_units == 0 and balance < 0
        )
        consistent = consistent and valid
    if current:
        expected = current.invoice.original.minor_units - sum(item.minor_units for item in facts)
        consistent = consistent and (
            current.invoice.outstanding.minor_units == expected
            or current.invoice.outstanding.minor_units == 0
            and expected < 0
        )
        revision_verified = invoice.source_revision == current.invoice.stamp.revision
        if historical_observation is not None:
            observed = InvoiceData.model_validate_json(json.dumps(historical_observation.invoice_data))
            provenance = historical_observation.provenance.get("invoice")
            revision_verified = (
                historical_observation.tenant_id == invoice.tenant_id
                and historical_observation.provider_connection_id == invoice.provider_connection_id
                and historical_observation.invoice_id == invoice.id
                and historical_observation.observed_at <= as_of
                and historical_observation.created_at <= as_of
                and isinstance(provenance, dict)
                and provenance.get("authenticity") == "VERIFIED"
                and provenance.get("currentness") == "CURRENT"
                and observed.model_dump(exclude={"stamp"}) == current.invoice.model_dump(exclude={"stamp"})
                and observed.stamp.model_dump(exclude={"observed_at"})
                == current.invoice.stamp.model_dump(exclude={"observed_at"})
            )
        consistent = consistent and revision_verified
    original = first.invoice.original.minor_units if first else invoice.original_minor
    # A missing first source revision is unavailable; the value above is never used as an opening fallback.
    opening = (
        original - sum(row.minor_units for row in facts if row.effective_at < start)
        if source.history_complete and first and before_d
        else None
    )
    before_h_balance = (
        max(0, original - sum(row.minor_units for row in facts if row.effective_at < end))
        if first and before_h
        else None
    )
    if frozen is not None:
        consistent = (
            consistent
            and original == frozen.original_minor
            and opening == frozen.opening_minor
            and history.timezone == frozen.timezone
            and invoice.currency == frozen.currency
            and invoice.exponent == frozen.exponent
        )
        opening, original = frozen.opening_minor, frozen.original_minor
    return CohortHistory(
        invoice_id=invoice.id,
        original_due_date=frozen.original_due_date if frozen else source.original_due_date,
        issue_date=frozen.issue_date if frozen else source.issue_date,
        timezone=history.timezone,
        original_minor=original,
        opening_minor=opening,
        currency=invoice.currency,
        exponent=invoice.exponent,
        outstanding_before_h=before_h_balance,
        outstanding_as_of=current.invoice.outstanding.minor_units if current else None,
        complete=complete,
        source_watermark=history.source_watermark,
        as_of_at=as_of,
        allocations=facts,
        opening_status=frozen.opening_status
        if frozen
        else before_d[-1].invoice.status
        if before_d
        else "unknown",
        source_consistent=consistent,
    )
