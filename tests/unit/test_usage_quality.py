"""T06 Decimal validation; missing/invalid prices cannot become free operations."""

from decimal import Decimal

import pytest

from aco.core.types import CostQuality, ProviderCost


def test_unknown_charge_is_not_zero() -> None:
    unknown = ProviderCost(amount=None, currency="USD", quality=CostQuality.UNKNOWN)
    assert unknown.amount is None and unknown.quality == "UNKNOWN"
    with pytest.raises(ValueError):
        ProviderCost(amount=Decimal(0), currency="USD", quality=CostQuality.UNKNOWN)


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1", "0.0000000000001", "1e26"])
def test_invalid_charge_is_rejected_before_database_rounding(amount: str) -> None:
    with pytest.raises(ValueError):
        ProviderCost(amount=Decimal(amount), currency="USD", quality=CostQuality.PROVIDER_REPORTED)
