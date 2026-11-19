"""Customer-facing policy reminder text built from the reviewed template and current invoice."""

from datetime import date

from aco.core.money import format_minor

SYNTHETIC_NOTICE = "This message concerns synthetic demonstration data."


def reminder_body(
    template_body: str,
    invoice_number: str,
    balance_minor: int,
    currency: str,
    exponent: int,
    due_date: date,
    synthetic: bool,
) -> str:
    body = (
        f"{template_body}\n\nInvoice: {invoice_number}"
        f"\nOutstanding: {currency} {format_minor(balance_minor, exponent)}"
        f"\nDue: {due_date.isoformat()}"
    )
    return body + f"\n\n{SYNTHETIC_NOTICE}" if synthetic else body
