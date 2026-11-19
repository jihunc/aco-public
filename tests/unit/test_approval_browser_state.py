"""Approval browser fixture cannot widen into live or arbitrary workspaces."""

from datetime import timedelta
from uuid import uuid4

import pytest
from approval_browser_state import require_fresh_workspace, require_local_profile

from aco.core.settings import Settings
from aco.features.identity.models import Tenant, now


@pytest.mark.parametrize("profile", [None, "deployed", "synthetic-agents", "synthetic-reminders"])
def test_approval_fixture_refuses_other_profiles(profile: str | None) -> None:
    with pytest.raises(ValueError, match="mock"):
        require_local_profile(profile, Settings())


@pytest.mark.parametrize(
    "field,value",
    [
        ("outbound_enabled", True),
        ("kill_switch", False),
        ("external_providers", "live"),
        ("app_env", "production"),
        ("synthetic_email_enabled", True),
    ],
)
def test_approval_fixture_refuses_unsafe_runtime(field: str, value: str | bool) -> None:
    with pytest.raises(ValueError, match="offline"):
        require_local_profile("mock", Settings().model_copy(update={field: value}))


def test_approval_fixture_allows_only_mock_outbound_off() -> None:
    require_local_profile("mock", Settings())


@pytest.mark.parametrize("changed", ["name", "old", "demo", "automation", "outbound"])
def test_approval_fixture_refuses_existing_or_uncontained_workspace(changed: str) -> None:
    tenant = Tenant(
        name=f"Approval browser {uuid4()}",
        is_demo=True,
        automation_enabled=False,
        outbound_paused=True,
        created_at=now(),
    )
    if changed == "name":
        tenant.name = "Existing customer workspace"
    elif changed == "old":
        tenant.created_at = now() - timedelta(minutes=16)
    elif changed == "demo":
        tenant.is_demo = False
    elif changed == "automation":
        tenant.automation_enabled = True
    else:
        tenant.outbound_paused = False
    with pytest.raises(ValueError, match="fresh synthetic"):
        require_fresh_workspace(tenant)


def test_approval_fixture_accepts_fresh_unique_paused_workspace() -> None:
    require_fresh_workspace(
        Tenant(
            name=f"Approval browser {uuid4()}",
            is_demo=True,
            automation_enabled=False,
            outbound_paused=True,
            created_at=now(),
        )
    )


def test_approval_fixture_refuses_remote_database_before_connecting() -> None:
    from pydantic import SecretStr

    with pytest.raises(ValueError, match="local application database"):
        require_local_profile(
            "mock", Settings(database_url=SecretStr("postgresql+psycopg://u:p@remote.example.test/aco"))
        )


@pytest.mark.parametrize(
    "host,port,allowed",
    [("172.18.0.2", 5432, True), ("172.18.0.3", 5432, False), ("172.18.0.2", 55432, False)],
)
def test_approval_fixture_database_matches_exact_control_plane_ip(
    monkeypatch: pytest.MonkeyPatch, host: str, port: int, allowed: bool
) -> None:
    from pydantic import SecretStr

    monkeypatch.setenv("ACO_LOCAL_PG_IP", "172.18.0.2")
    settings = Settings(database_url=SecretStr(f"postgresql+psycopg://aco:synthetic@{host}:{port}/aco"))
    if allowed:
        require_local_profile("mock", settings)
    else:
        with pytest.raises(ValueError, match="local application database"):
            require_local_profile("mock", settings)


@pytest.mark.parametrize("trusted", ["127.0.0.1", "169.254.1.2", "224.0.0.1", "8.8.8.8", "0.0.0.0"])
def test_approval_fixture_rejects_untrusted_control_plane_address(
    monkeypatch: pytest.MonkeyPatch, trusted: str
) -> None:
    from pydantic import SecretStr

    monkeypatch.setenv("ACO_LOCAL_PG_IP", trusted)
    with pytest.raises(ValueError, match="local application database"):
        require_local_profile(
            "mock", Settings(database_url=SecretStr(f"postgresql+psycopg://aco:synthetic@{trusted}:5432/aco"))
        )


@pytest.mark.parametrize(
    "issuer,allowed",
    [
        ("http://172.18.0.4:8080/realms/aco", True),
        ("http://172.18.0.5:8080/realms/aco", False),
        ("http://172.18.0.4:58080/realms/aco", False),
        ("http://172.18.0.4:8080/realms/other", False),
        ("https://172.18.0.4:8080/realms/aco", False),
    ],
)
def test_approval_fixture_issuer_matches_exact_control_plane_topology(
    monkeypatch: pytest.MonkeyPatch, issuer: str, allowed: bool
) -> None:
    from approval_browser_state import local_issuer

    monkeypatch.setenv("ACO_LOCAL_OIDC_IP", "172.18.0.4")
    assert local_issuer(issuer) is allowed
