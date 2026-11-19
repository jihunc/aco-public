"""The local live runtime is explicit, server-only and bound to current private limits."""

import importlib
from pathlib import Path
from types import ModuleType
from uuid import UUID, uuid4

import pytest
from provider_smoke_support import private_config

from aco.core.live import DELIVERY_LIVE_AUTHORIZATION
from aco.core.settings import Settings


def loader() -> ModuleType:
    return importlib.import_module("aco.integrations.openai.application")


def current_selection(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    path: Path,
    grant: UUID,
) -> dict[str, str]:
    # Unit-only seams; actual persisted allocations are tested separately.
    monkeypatch.setattr(module, "current_source_commit", lambda: "a" * 40)
    monkeypatch.setattr(module, "verify_grant_allocation", lambda *args, **kwargs: None)
    return {
        "ACO_AGENT_LIVE_CONFIG_PATH": str(path),
        "ACO_AGENT_LIVE_GRANT_ID": str(grant),
        "ACO_LIVE_AUTHORIZATION_REF": DELIVERY_LIVE_AUTHORIZATION,
        "ACO_LIVE_CAMPAIGN_FILE": str(path.parent / "unit-only-ledger"),
        "ACO_LIVE_ALLOCATION_ID": str(uuid4()),
    }


def test_default_runtime_does_not_read_live_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    module = loader()
    assert module.local_agent_runtime(Settings(), {}) is None


def test_explicit_runtime_rechecks_private_limits_and_kill_before_each_wire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = loader()
    path = private_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    selected = uuid4()
    verified = []
    monkeypatch.setattr(module, "verify_grant", lambda settings, grant_id, config: verified.append(grant_id))
    runtime = module.local_agent_runtime(Settings(), current_selection(module, monkeypatch, path, selected))
    assert runtime is not None
    assert runtime.grant_id == selected and verified == [selected]
    assert not runtime.budget.kill_switch()
    assert "fake-secret" not in repr(runtime.settings)
    path.write_text(
        path.read_text().replace("ACO_LIVE_MAX_INPUT_TOKENS=2048", "ACO_LIVE_MAX_INPUT_TOKENS=1024")
    )
    assert runtime.budget.kill_switch()
    path.write_text(path.read_text().replace("ACO_LIVE_KILL_SWITCH=false", "ACO_LIVE_KILL_SWITCH=true"))
    assert runtime.budget.kill_switch()


def test_invalid_selected_runtime_has_fixed_safe_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = loader()
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="^BOUNDED_LIVE_CONFIGURATION_BLOCKED$"):
        module.local_agent_runtime(Settings(), {"ACO_AGENT_LIVE_GRANT_ID": "not-a-uuid"})


def test_investigation_limits_are_separate_from_provider_preflight(tmp_path: Path) -> None:
    from aco.integrations.provider_smoke.config import AUTHORIZATION_REF, SmokeBlocked, load_config

    path = private_config(tmp_path)
    path.write_text(path.read_text().replace("=2048", "=32000").replace("=512", "=2048"))
    with pytest.raises(SmokeBlocked, match="INVALID_LIMITS"):
        load_config(path, authorization_ref=AUTHORIZATION_REF)
    loaded = load_config(path, authorization_ref=AUTHORIZATION_REF, lane="investigation")
    assert (loaded.max_requests, loaded.max_input_tokens, loaded.max_output_tokens) == (3, 32000, 2048)
    for before, after in (("=32000", "=32001"), ("=2048", "=2049"), ("=3\n", "=4\n")):
        original = path.read_text()
        path.write_text(original.replace(before, after))
        with pytest.raises(SmokeBlocked, match="INVALID_LIMITS"):
            load_config(path, authorization_ref=AUTHORIZATION_REF, lane="investigation")
        path.write_text(original)


def test_explicit_private_path_keeps_keys_outside_delivery_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = loader()
    path = private_config(tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.chdir(checkout)
    monkeypatch.setattr(module, "verify_grant", lambda settings, grant_id, config: None)
    values = {"ACO_AGENT_LIVE_CONFIG_PATH": str(path)}
    with pytest.raises(RuntimeError, match="BOUNDED_LIVE_CONFIGURATION_BLOCKED"):
        module.local_agent_runtime(Settings(), values)
    values = current_selection(module, monkeypatch, path, uuid4())
    runtime = module.local_agent_runtime(Settings(), values)
    assert runtime is not None and not runtime.budget.kill_switch()
    assert not (checkout / ".env.live").exists()
    path.write_text(path.read_text().replace("ACO_LIVE_KILL_SWITCH=false", "ACO_LIVE_KILL_SWITCH=true"))
    assert runtime.budget.kill_switch()


def test_new_delivery_authorization_requires_durable_matching_campaign_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aco.integrations.provider_smoke.campaign import AUTHORIZATION_REF

    module = loader()
    path = private_config(tmp_path)
    monkeypatch.setattr(module, "verify_grant", lambda settings, grant_id, config: None)
    with pytest.raises(RuntimeError, match="BOUNDED_LIVE_CONFIGURATION_BLOCKED"):
        module.local_agent_runtime(
            Settings(),
            {
                "ACO_AGENT_LIVE_GRANT_ID": str(uuid4()),
                "ACO_AGENT_LIVE_CONFIG_PATH": str(path),
                "ACO_LIVE_AUTHORIZATION_REF": AUTHORIZATION_REF,
            },
        )


def test_current_delivery_investigation_allows_explicit_schema_exposure_only(tmp_path: Path) -> None:
    from datetime import UTC, datetime
    from decimal import Decimal

    from aco.core.live import DELIVERY_LIVE_AUTHORIZATION
    from aco.integrations.provider_smoke.config import AUTHORIZATION_REF, SmokeBlocked, load_config
    from aco.integrations.provider_smoke.transport import price_bound

    path = private_config(tmp_path)
    content = path.read_text().replace("=2048", "=64000").replace("=512", "=2048")
    path.write_text(content)
    current = load_config(path, authorization_ref=DELIVERY_LIVE_AUTHORIZATION, lane="investigation")
    assert (current.max_requests, current.max_input_tokens, current.max_output_tokens) == (3, 64000, 2048)
    assert price_bound(current, now=datetime(2026, 11, 19, tzinfo=UTC)) == Decimal("0.34048")
    # The consumed one-investigation approval and ordinary provider preflight stay bounded.
    with pytest.raises(SmokeBlocked, match="INVALID_LIMITS"):
        load_config(path, authorization_ref=AUTHORIZATION_REF, lane="investigation")
    with pytest.raises(SmokeBlocked, match="AUTHORIZATION_REQUIRED"):
        load_config(path, authorization_ref=DELIVERY_LIVE_AUTHORIZATION)
    for before, after in (("=64000", "=64001"), ("=2048", "=2049"), ("=3\n", "=4\n")):
        path.write_text(content.replace(before, after))
        with pytest.raises(SmokeBlocked, match="INVALID_LIMITS"):
            load_config(path, authorization_ref=DELIVERY_LIVE_AUTHORIZATION, lane="investigation")
    path.write_text(content.replace("ACO_LIVE_KILL_SWITCH=false", "ACO_LIVE_KILL_SWITCH=true"))
    with pytest.raises(SmokeBlocked, match="SMOKE_KILL_SWITCH_ENABLED"):
        load_config(path, authorization_ref=DELIVERY_LIVE_AUTHORIZATION, lane="investigation")
