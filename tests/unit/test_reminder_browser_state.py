"""The test-only storage setup cannot be redirected or run from default lanes."""

from typing import Any

import pytest
from reminder_browser_state import require_local_profile


@pytest.mark.parametrize(
    ("profile", "endpoint", "bucket"),
    [
        (None, "http://127.0.0.1:59000", "aco-artifacts"),
        ("mock", "http://127.0.0.1:59000", "aco-artifacts"),
        ("synthetic-reminders", "https://remote.example.test", "aco-artifacts"),
        ("synthetic-reminders", "http://127.0.0.1:59000", "other-bucket"),
    ],
)
def test_browser_storage_helper_rejects_unapproved_profiles_and_targets(
    profile: Any, endpoint: Any, bucket: Any
) -> None:
    with pytest.raises(ValueError):
        require_local_profile(profile, endpoint, bucket)


@pytest.mark.parametrize("endpoint", ["http://127.0.0.1:59000", "http://storage:8333"])
def test_browser_storage_helper_allows_only_reviewed_local_app_targets(endpoint: Any) -> None:
    require_local_profile("synthetic-reminders", endpoint, "aco-artifacts")


def test_browser_storage_helper_accepts_only_exact_verified_container_ip(monkeypatch: Any) -> None:
    monkeypatch.setenv("ACO_LOCAL_S3_IP", "172.18.0.2")
    require_local_profile("synthetic-reminders", "http://172.18.0.2:8333", "aco-artifacts")
    with pytest.raises(ValueError):
        require_local_profile("synthetic-reminders", "http://172.18.0.3:8333", "aco-artifacts")
