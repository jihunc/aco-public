"""Exact synthetic reset compensation arithmetic; never infer cash from invoice status."""

import importlib.util
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from aco.core.types import Money, SourceStamp
from aco.features.accounting.dto import InvoiceData, PaymentData

if TYPE_CHECKING:
    from aco.features.accounting.demo_restoration import ResidualCash


CONNECTION = UUID("147724c1-a6d0-4fd6-b038-66b4a90ba890")
STAMP = SourceStamp(
    connection_id=CONNECTION,
    external_id="INV-101",
    revision="1",
    observed_at=datetime(2026, 11, 19, tzinfo=UTC),
    source_at=datetime(2026, 11, 19, tzinfo=UTC),
    synthetic=True,
)


def invoice(balance: int = 10000) -> InvoiceData:
    return InvoiceData(
        external_id="INV-101",
        external_customer_id="CUSTOMER-1",
        number="INV-101",
        issue_date=STAMP.observed_at.date(),
        due_date=STAMP.observed_at.date(),
        original=Money(minor_units=10000, currency="AUD", exponent=2),
        outstanding=Money(minor_units=balance, currency="AUD", exponent=2),
        status="paid" if not balance else "open",
        source_status="SYNTHETIC",
        organisation_id="synthetic:account",
        stamp=STAMP,
        verified=True,
    )


def payment(key: str, amount: int, parent: str | None = None) -> PaymentData:
    return PaymentData(
        external_payment_id=key,
        external_allocation_id="1",
        external_invoice_id="INV-101",
        kind="reversal" if parent else "cash",
        amount=Money(minor_units=amount, currency="AUD", exponent=2),
        parent_payment_id=parent,
        parent_allocation_id="1" if parent else None,
        organisation_id="synthetic:account",
        stamp=STAMP.model_copy(update={"external_id": key}),
        verified=True,
    )


def planner() -> Callable[[InvoiceData, InvoiceData, list[PaymentData]], tuple["ResidualCash", ...]]:
    assert importlib.util.find_spec("aco.features.accounting.demo_restoration") is not None, (
        "Canonical source reset restoration is not implemented"
    )
    from aco.features.accounting.demo_restoration import residual_reversals

    return residual_reversals


def test_residual_cash_only_reversed_once_with_exact_parent_identity() -> None:
    build = planner()
    seed = invoice()
    current = invoice(2500)
    cash = [payment("cash-a", 5000), payment("cash-b", 5000), payment("refund-a", -2500, "cash-a")]
    result = build(seed, current, cash)
    assert [(row.external_payment_id, row.external_allocation_id, row.minor_units) for row in result] == [
        ("cash-a", "1", 2500),
        ("cash-b", "1", 5000),
    ]
    restored = cash + [payment("reset-a", -2500, "cash-a"), payment("reset-b", -5000, "cash-b")]
    assert build(seed, seed, restored) == ()


def test_seed_cash_is_preserved_without_becoming_new_post_confirmation_cash() -> None:
    build = planner()
    seed = invoice(5000)
    assert (
        build(seed, invoice(2500), [payment("seed-INV-101", 5000), payment("cash-new", 2500)])[0].minor_units
        == 2500
    )
    with pytest.raises(HTTPException, match="DEMO_RESET_SOURCE_REVIEW_REQUIRED"):
        build(
            seed,
            invoice(6000),
            [payment("seed-INV-101", 5000), payment("reverse-seed", -1000, "seed-INV-101")],
        )


@pytest.mark.parametrize(
    "failure",
    [
        "foreign_connection",
        "wrong_invoice",
        "currency",
        "unverified",
        "duplicate",
        "missing_parent",
        "over_reversed",
        "inconsistent_balance",
    ],
)
def test_ambiguous_or_incomplete_source_cannot_be_restored(failure: str) -> None:
    build = planner()
    seed, current, cash = invoice(), invoice(5000), [payment("new", 5000)]
    if failure == "foreign_connection":
        cash[0] = cash[0].model_copy(update={"stamp": STAMP.model_copy(update={"connection_id": uuid4()})})
    elif failure == "wrong_invoice":
        cash[0] = cash[0].model_copy(update={"external_invoice_id": "INV-OTHER"})
    elif failure == "currency":
        cash[0] = cash[0].model_copy(update={"amount": Money(minor_units=5000, currency="USD", exponent=2)})
    elif failure == "unverified":
        cash[0] = cash[0].model_copy(update={"verified": False})
    elif failure == "duplicate":
        cash.append(cash[0])
    elif failure == "missing_parent":
        cash.append(payment("bad", -1000, "missing"))
    elif failure == "over_reversed":
        cash.append(payment("bad", -6000, "new"))
    elif failure == "inconsistent_balance":
        current = invoice(4000)
    with pytest.raises(HTTPException, match="DEMO_RESET_SOURCE_REVIEW_REQUIRED"):
        build(seed, current, cash)
