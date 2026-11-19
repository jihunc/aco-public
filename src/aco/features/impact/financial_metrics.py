"""Ordinary reporting-window cash only; this does not reconstruct or award mature cohorts."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from iso4217 import Currency

from aco.features.impact.dto import CashEvidence, CashGroup, CashIssue
from aco.features.receivables.queries import MoneyJSON


@dataclass(frozen=True)
class CashFact:
    # Constructed from the append-only VerifiedPaymentAllocation table, not claims or hints.
    id: UUID
    tenant_id: UUID
    connection_id: UUID
    invoice_id: UUID
    organisation_id: str
    currency: str
    exponent: int
    synthetic: bool
    kind: str
    minor_units: int
    parent_id: UUID | None
    effective_at: datetime
    observed_at: datetime
    imported_at: datetime
    source_revision: str


@dataclass(frozen=True)
class InvoiceBasis:
    id: UUID
    tenant_id: UUID
    connection_id: UUID
    organisation_id: str
    currency: str
    exponent: int
    synthetic: bool
    freshness: Literal["fresh", "stale", "unknown"]
    last_synced_at: datetime | None


def known_cash(
    tenant_id: UUID,
    since: datetime,
    until: datetime,
    as_of: datetime,
    facts: list[CashFact],
    invoices: dict[UUID, InvoiceBasis],
) -> list[CashGroup]:
    # Source uniqueness is tenant+connection+payment+allocation; the persisted ID is its
    # immutable projection identity. Re-reading or joining a fact cannot duplicate it.
    available = {fact.id: fact for fact in facts if fact.tenant_id == tenant_id and fact.imported_at <= as_of}

    def basis_error(fact: CashFact) -> str | None:
        invoice = invoices.get(fact.invoice_id)
        if invoice is None or (
            invoice.tenant_id,
            invoice.connection_id,
            invoice.organisation_id,
            invoice.currency,
            invoice.exponent,
            invoice.synthetic,
        ) != (
            tenant_id,
            fact.connection_id,
            fact.organisation_id,
            fact.currency,
            fact.exponent,
            fact.synthetic,
        ):
            return "SOURCE_SCOPE_MISMATCH"
        try:
            if Currency(fact.currency).exponent != fact.exponent:
                return "UNSUPPORTED_CURRENCY_BASIS"
        except ValueError:
            return "UNSUPPORTED_CURRENCY_BASIS"
        if fact.observed_at > as_of:
            return "FUTURE_OBSERVATION"
        if not fact.source_revision:
            return "MISSING_SOURCE_REVISION"
        return None

    def parent_error(fact: CashFact) -> str | None:
        parent = available.get(fact.parent_id) if fact.parent_id else None
        if (
            parent is None
            or basis_error(parent)
            or parent.kind != "cash"
            or parent.minor_units <= 0
            or parent.parent_id is not None
        ):
            return "PARENT_NOT_VERIFIED_IN_SCOPE"
        if (
            parent.connection_id,
            parent.invoice_id,
            parent.organisation_id,
            parent.currency,
            parent.exponent,
            parent.synthetic,
        ) != (
            fact.connection_id,
            fact.invoice_id,
            fact.organisation_id,
            fact.currency,
            fact.exponent,
            fact.synthetic,
        ):
            return "PARENT_SCOPE_MISMATCH"
        if parent.effective_at > fact.effective_at:
            return "PARENT_AFTER_ADJUSTMENT"
        return None

    linked_totals: dict[UUID, int] = {}
    for fact in available.values():
        if (
            fact.kind in {"refund", "reversal"}
            and fact.minor_units < 0
            and fact.effective_at <= as_of
            and not basis_error(fact)
            and not parent_error(fact)
        ):
            assert fact.parent_id is not None
            linked_totals[fact.parent_id] = linked_totals.get(fact.parent_id, 0) - fact.minor_units
    groups: dict[tuple[str, int, bool], list[CashFact]] = {}
    for fact in available.values():
        if since <= fact.effective_at < until:
            groups.setdefault((fact.currency, fact.exponent, fact.synthetic), []).append(fact)
    result = []
    for (currency, exponent, synthetic), selected in sorted(groups.items()):
        gross = outflow = count = non_cash = 0
        issues: list[CashIssue] = []
        evidence: list[CashEvidence] = []
        states: list[str] = []
        reads: list[datetime] = []
        for fact in sorted(selected, key=lambda value: (value.effective_at, str(value.id))):
            invoice = invoices.get(fact.invoice_id)
            states.append(invoice.freshness if invoice else "unknown")
            if invoice and invoice.last_synced_at:
                reads.append(invoice.last_synced_at)
            reason = basis_error(fact)
            if fact.kind in {"credit", "write_off"}:
                non_cash += 1
                # These facts never contribute cash, even if their unsupported scope is flagged.
            elif fact.kind == "cash":
                if fact.minor_units <= 0 or fact.parent_id is not None:
                    reason = reason or "INVALID_CASH_FACT"
            elif fact.kind in {"refund", "reversal"}:
                reason = reason or ("INVALID_OUTFLOW_SIGN" if fact.minor_units >= 0 else parent_error(fact))
                if (
                    not reason
                    and fact.parent_id
                    and linked_totals[fact.parent_id] > available[fact.parent_id].minor_units
                ):
                    reason = "PARENT_OVERDRAWN"
            else:
                reason = reason or "UNSUPPORTED_ALLOCATION_KIND"
            included = not reason and fact.kind in {"cash", "refund", "reversal"}
            if reason:
                issues.append(CashIssue(allocation_id=fact.id, invoice_id=fact.invoice_id, reason=reason))
            if included:
                count += 1
                if fact.kind == "cash":
                    gross += fact.minor_units
                else:
                    outflow -= fact.minor_units
            evidence.append(
                CashEvidence(
                    allocation_id=fact.id,
                    invoice_id=fact.invoice_id,
                    provider_connection_id=fact.connection_id,
                    source_revision=fact.source_revision,
                    kind=fact.kind,
                    minor_units=str(fact.minor_units),
                    effective_at=fact.effective_at,
                    observed_at=fact.observed_at,
                    imported_at=fact.imported_at,
                    parent_id=fact.parent_id,
                    included=bool(included),
                )
            )
        try:
            supported = Currency(currency).exponent == exponent
        except ValueError:
            supported = False
        result.append(
            CashGroup(
                currency=currency,
                exponent=exponent,
                synthetic=synthetic,
                cash=_money(gross, currency, exponent) if supported else None,
                outflows=_money(outflow, currency, exponent) if supported else None,
                net_cash=_money(gross - outflow, currency, exponent)
                if supported and count and not issues
                else None,
                allocation_count=count,
                non_cash_count=non_cash,
                excluded_count=len(issues),
                completeness="partial" if count and not issues else "unavailable",
                freshness="unknown" if "unknown" in states else "stale" if "stale" in states else "fresh",
                last_synced_at=min(reads) if len(reads) == len(selected) else None,
                issues=issues,
                evidence=evidence,
            )
        )
    return result


def _money(value: int, currency: str, exponent: int) -> MoneyJSON:
    return MoneyJSON(minor_units=value, currency=currency, exponent=exponent)
