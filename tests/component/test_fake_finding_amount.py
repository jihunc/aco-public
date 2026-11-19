"""Simulated investigation findings show exact human-readable money, not bare minor units."""

import pytest

from aco.integrations.fake.investigation import _major


@pytest.mark.parametrize(
    ("minor", "exponent", "expected"),
    [
        (10000, 2, "100.00"),
        (-500, 2, "-5.00"),
        (5, 2, "0.05"),
        (0, 2, "0.00"),
        (123, 0, "123"),
        (1, 3, "0.001"),
    ],
)
def test_minor_units_render_as_exact_major_amount(minor: int, exponent: int, expected: str) -> None:
    assert _major(minor, exponent) == expected
