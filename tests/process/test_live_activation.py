"""Catch marker/configuration pretending to be scoped external-run authorization."""

from datetime import UTC, datetime

import pytest

from aco.core.live import LiveActivationBlocked, authorize_live_profile


def test_missing_required_live_setting_blocks_activation() -> None:
    with pytest.raises(LiveActivationBlocked, match="authorization"):
        authorize_live_profile({}, now=datetime(2026, 11, 17, tzinfo=UTC))


def test_credentials_and_marker_never_authorize_external_calls() -> None:
    with pytest.raises(LiveActivationBlocked):
        authorize_live_profile(
            {"OPENAI_API_KEY": "synthetic-never-real", "ACO_TEST_PROFILE": "live"},
            now=datetime(2026, 11, 17, tzinfo=UTC),
        )
