"""All configurations launch the same existing Docker application, never host processes."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_development_config import configuration


def test_backend_uses_existing_docker_service_dns(tmp_path: Path) -> None:
    from aco.core.development import load_development

    selected = load_development(configuration(tmp_path))
    backend = selected.backend_environment()
    assert backend["DATABASE_URL"].endswith("@postgres:5432/aco")
    assert backend["STORAGE_ENDPOINT"] == "http://storage:8333"
    assert backend["OIDC_ISSUER"] == "http://identity.aco.localhost:58480/realms/aco"
    assert selected.web_environment()["ACO_API_INTERNAL_ORIGIN"] == "http://api:8000"


def test_compose_reuses_product_stack_without_ambient_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aco.core.development import load_development
    from aco.development import compose

    monkeypatch.setenv("OPENAI_API_KEY", "ambient-never-forward")
    monkeypatch.setenv("COMPOSE_FILE", "unexpected.yaml")
    captured: dict[str, object] = {}

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured.update(command=command, **kwargs)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    compose(load_development(configuration(tmp_path)), "stop")
    command = captured["command"]
    assert isinstance(command, list)
    assert command[:2] == ["docker", "compose"]
    assert any(str(value).endswith("compose.product.yaml") for value in command)
    assert command[-1] == "stop"
    env = captured["env"]
    assert isinstance(env, dict)
    assert "OPENAI_API_KEY" not in env and "COMPOSE_FILE" not in env
    assert "ambient-never-forward" not in repr(captured)


def test_initialization_is_private_fresh_without_host_dependencies(tmp_path: Path) -> None:
    from aco.core.development import load_development
    from aco.development import initialize

    target = tmp_path / "development.env"
    initialize(target, "offline", root=Path.cwd(), output=tmp_path / "infra", project="aco-product-dev-fresh")
    selected = load_development(target)
    assert selected.settings().synthetic_email_enabled
    assert target.stat().st_mode & 0o077 == 0
    assert selected.infra_path.stat().st_mode & 0o077 == 0
    assert selected.provider_directory.is_dir()
    prior = target.read_bytes()
    with pytest.raises((ValueError, FileExistsError)):
        initialize(target, "live", root=Path.cwd(), output=tmp_path / "infra")
    assert target.read_bytes() == prior


def test_stop_never_destroys_volumes_or_runs_provider_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aco.development import main

    target = configuration(tmp_path)
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr("aco.development.compose", lambda config, *args: commands.append(args))
    monkeypatch.setattr(sys, "argv", ["dev", "down", "--config", str(target)])
    main()
    assert commands == [("stop",)]


def test_cli_errors_never_echo_private_values(tmp_path: Path) -> None:
    target = tmp_path / "invalid.env"
    target.write_text("OPENAI_API_KEY=synthetic-do-not-echo\nDEV_MODE=live\n")
    target.chmod(0o600)
    result = subprocess.run(
        [sys.executable, "scripts/dev.py", "check", "--config", str(target)],
        capture_output=True, text=True,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(Path.home())},
    )
    assert result.returncode == 2
    assert "synthetic-do-not-echo" not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr


def test_up_is_docker_only_and_explicit_migration_before_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aco.core.development import load_development
    from aco.development import up

    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr("aco.development.compose", lambda config, *args: commands.append(args))
    assert up(load_development(configuration(tmp_path))) == 0
    assert ("build",) in commands
    assert ("run", "--rm", "migrate") in commands
    last = commands[-1]
    assert last[:2] == ("up", "-d")
    assert set(("api", "worker", "web", "product-ingress")) <= set(last)
    assert not any("sandbox-exec" in str(command) for command in commands)


def test_restart_reloads_selection_instead_of_restarting_old_provider_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aco.development import main

    path = configuration(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr("aco.development.compose", lambda config, *args: calls.append(args[0]))
    def fresh_up(config: object) -> int:
        calls.append("fresh-up")
        return 0

    monkeypatch.setattr("aco.development.up", fresh_up)
    monkeypatch.setattr(sys, "argv", ["dev", "restart", "--config", str(path)])
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 0
    assert calls == ["fresh-up"]


def test_up_stops_previous_selection_before_new_configuration_can_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aco.core.development import load_development
    from aco.development import up

    calls: list[str] = []

    def compose(config: object, *args: str) -> None:
        calls.append(args[0])
        if args[0] == "build":
            raise ValueError("new selection refuses")

    monkeypatch.setattr("aco.development.compose", compose)
    with pytest.raises(ValueError, match="new selection refuses"):
        up(load_development(configuration(tmp_path)))
    assert calls == ["stop", "build"]
