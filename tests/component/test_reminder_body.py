"""Policy reminder bodies show exact human-readable money and label synthetic invoices only."""

from datetime import date

from aco.core.money import format_minor
from aco.features.communications.reminder_text import reminder_body


def test_body_shows_major_amount_and_due_date() -> None:
    body = reminder_body("Please review the balance.", "INV-108", 6000, "AUD", 2, date(2026, 11, 26), False)
    assert body == ("Please review the balance.\n\nInvoice: INV-108\nOutstanding: AUD 60.00\nDue: 2026-11-26")
    assert "minor units" not in body


def test_synthetic_invoice_is_labeled() -> None:
    body = reminder_body("Hi.", "INV-1", 5, "AUD", 2, date(2026, 1, 2), True)
    assert body.endswith("\n\nThis message concerns synthetic demonstration data.")


def test_format_minor_is_exact_for_large_and_zero_exponent_values() -> None:
    assert format_minor(10000000000000001, 2) == "100000000000000.01"
    assert format_minor(-5, 2) == "-0.05"
    assert format_minor(1500, 0) == "1500"
