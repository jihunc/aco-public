import pytest


@pytest.mark.live_provider
def test_forbidden_live_sentinel() -> None:
    raise AssertionError("Live sentinel must never execute")
