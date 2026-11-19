"""Independent reporting-window arithmetic; no cohort or complete-history acceptance."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from aco.features.impact.financial_metrics import CashFact, InvoiceBasis, known_cash

AT = datetime(2026, 11, 18, tzinfo=UTC)
START, END = AT - timedelta(days=7), AT
TENANT, CONNECTION, INVOICE = uuid4(), uuid4(), uuid4()
BASIS = InvoiceBasis(INVOICE, TENANT, CONNECTION, "org", "AUD", 2, True, "fresh", AT)


def fact(
    kind: str,
    amount: int,
    *,
    parent_id: UUID | None = None,
    effective_at: datetime = START,
    observed_at: datetime = AT - timedelta(seconds=1),
) -> CashFact:
    return CashFact(
        uuid4(),
        TENANT,
        CONNECTION,
        INVOICE,
        "org",
        "AUD",
        2,
        True,
        kind,
        amount,
        parent_id,
        effective_at,
        observed_at,
        AT - timedelta(seconds=1),
        "r1",
    )


def test_own_effective_window_includes_start_excludes_end_and_credits_are_not_cash() -> None:
    cash = fact("cash", 10000)
    refund = fact("refund", -2500, parent_id=cash.id, effective_at=START + timedelta(days=1))
    rows = [cash, refund, fact("cash", 9900, effective_at=END), fact("credit", 3000), fact("write_off", 2000)]
    view = known_cash(TENANT, START, END, AT, rows, {INVOICE: BASIS})
    group = view[0]
    assert group.net_cash is not None and group.net_cash.minor_units == 7500
    assert group.cash is not None and group.cash.minor_units == 10000
    assert group.outflows is not None and group.outflows.minor_units == 2500
    assert group.non_cash_count == 2 and group.allocation_count == 2
    assert group.completeness == "partial"  # Imported facts are not a complete-history watermark.


def test_refund_parent_outside_window_is_valid_and_net_cash_can_be_negative() -> None:
    parent = fact("cash", 10000, effective_at=START - timedelta(days=10))
    refund = fact("reversal", -10000, parent_id=parent.id)
    result = known_cash(TENANT, START, END, AT, [parent, refund], {INVOICE: BASIS})[0]
    assert result.net_cash is not None and result.net_cash.minor_units == -10000
    assert result.allocation_count == 1


def test_wrong_scope_currency_or_missing_parent_never_borrows_cash() -> None:
    parent = fact("cash", 10000, effective_at=START - timedelta(days=1))
    child = fact("refund", -500, parent_id=parent.id)
    for changed in (
        replace(parent, tenant_id=uuid4()),
        replace(parent, connection_id=uuid4()),
        replace(parent, invoice_id=uuid4()),
        replace(parent, currency="USD"),
        replace(parent, exponent=3),
        replace(parent, synthetic=False),
        replace(parent, kind="credit"),
    ):
        group = known_cash(TENANT, START, END, AT, [changed, child], {INVOICE: BASIS})[0]
        assert group.net_cash is None and group.excluded_count == 1
        assert group.issues[0].allocation_id == child.id


def test_repeated_input_identity_cannot_double_count_and_large_minor_units_stay_exact() -> None:
    cash = fact("cash", 10000000000000001)
    result = known_cash(TENANT, START, END, AT, [cash, cash], {INVOICE: BASIS})[0]
    assert result.net_cash is not None and result.net_cash.minor_units == 10000000000000001
    assert result.model_dump(mode="json")["net_cash"]["minor_units"] == "10000000000000001"


def test_provenance_and_currency_are_separate_and_future_observation_is_not_verified_cash() -> None:
    a = fact("cash", 100)
    other_invoice = uuid4()
    b = replace(a, id=uuid4(), invoice_id=other_invoice, currency="JPY", exponent=0, synthetic=False)
    c = fact("cash", 900, observed_at=AT + timedelta(seconds=1))
    scopes = {
        INVOICE: BASIS,
        other_invoice: replace(BASIS, id=other_invoice, currency="JPY", exponent=0, synthetic=False),
    }
    groups = known_cash(TENANT, START, END, AT, [a, b, c], scopes)
    assert len(groups) == 2
    aud, jpy = groups
    assert aud.net_cash is None and aud.excluded_count == 1
    assert jpy.net_cash is not None and jpy.net_cash.minor_units == 100 and not jpy.synthetic


def test_overdrawn_linked_refunds_are_unavailable_instead_of_a_guessed_net() -> None:
    cash = fact("cash", 100)
    children = [fact("refund", -60, parent_id=cash.id), fact("reversal", -41, parent_id=cash.id)]
    group = known_cash(TENANT, START, END, AT, [cash, *children], {INVOICE: BASIS})[0]
    assert group.net_cash is None and group.excluded_count == 2
    assert all(issue.reason == "PARENT_OVERDRAWN" for issue in group.issues)
