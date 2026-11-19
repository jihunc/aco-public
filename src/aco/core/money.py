"""Exact fixed-point rendering of integer minor units; no floating-point rounding."""


def format_minor(minor: int, exponent: int) -> str:
    sign, digits = ("-" if minor < 0 else ""), str(abs(minor)).rjust(exponent + 1, "0")
    return f"{sign}{digits[:-exponent]}.{digits[-exponent:]}" if exponent else f"{sign}{digits}"
