"""Execute fresh private CI setup without services, local files or provider credentials."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]


def run_local_regression(
    path: Path,
    *,
    fail_at: str = "",
    existing_stack: bool = False,
    existing_volume: bool = False,
    bad_policy: bool = False,
    compose_selector: str = "",
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    """Execute the real orchestrator with disposable external-command boundaries."""
    (path / "scripts").mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts/test-local-regression", path / "scripts/test-local-regression")
    binary = path / "bin"
    binary.mkdir()
    calls = path / "calls.jsonl"
    fake = binary / "fake"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json,os,pathlib,sys\n"
        "name=pathlib.Path(sys.argv[0]).name\n"
        "args=[name,*sys.argv[1:]]\n"
        "with open(os.environ['CALLS'],'a') as f: f.write(json.dumps(args)+'\\n')\n"
        "if ' '.join(args)==os.environ['FAIL_AT']: sys.exit(23)\n"
        "if name=='uname': print('Linux')\n"
        "elif name=='git': print('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')\n"
        "elif name=='id': print('1001')\n"
        "elif name=='stat':\n"
        " print({'%u':'1001','%a':'600','%u:%g:%a':'1000:1000:600'}[sys.argv[2]])\n"
        "elif name=='docker' and sys.argv[1]=='ps' and os.environ['EXISTING_STACK']=='yes':\n"
        " print('existing-container')\n"
        "elif args[:3]==['docker','volume','ls'] and os.environ['EXISTING_VOLUME']=='yes':\n"
        " print('existing-volume')\n"
        "elif args==['python3','scripts/prepare-ci.py']:\n"
        " policy=pathlib.Path('.tooling/storage/s3-identities.json')\n"
        " policy.parent.mkdir(parents=True)\n"
        " policy.write_text('{}')\n"
        " policy.chmod(0o600)\n"
        " if os.environ['BAD_POLICY']=='yes':\n"
        "  policy.rename(policy.with_suffix('.original'))\n"
        "  policy.symlink_to('s3-identities.original')\n"
    )
    fake.chmod(0o700)
    for name in ("uname", "git", "id", "stat", "docker", "sudo", "python3"):
        (binary / name).symlink_to(fake)
    (path / "scripts/test-portable").symlink_to(fake)
    result = subprocess.run(
        ["/bin/sh", "scripts/test-local-regression"],
        cwd=path,
        env={
            "PATH": f"{binary}:/usr/bin:/bin",
            "CALLS": str(calls),
            "FAIL_AT": fail_at,
            "EXISTING_STACK": "yes" if existing_stack else "no",
            "EXISTING_VOLUME": "yes" if existing_volume else "no",
            "BAD_POLICY": "yes" if bad_policy else "no",
            **({compose_selector: "synthetic-override"} if compose_selector else {}),
        },
        text=True,
        capture_output=True,
    )
    return result, [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []


def fixture_root(path: Path) -> None:
    (path / "scripts").mkdir()
    shutil.copyfile(ROOT / ".env.example", path / ".env.example")
    shutil.copyfile(ROOT / "scripts/render_storage_config.py", path / "scripts/render_storage_config.py")


def prepare(path: Path) -> subprocess.CompletedProcess[str]:
    # Environment credential presence is irrelevant: setup must generate only local random values.
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/prepare-ci.py")],
        cwd=path,
        env={"PATH": os.environ.get("PATH", ""), "OPENAI_API_KEY": "synthetic-never-consumed"},
        text=True,
        capture_output=True,
    )


def test_fresh_ci_creates_independent_private_storage_and_disabled_provider_inputs(tmp_path: Path) -> None:
    fixture_root(tmp_path)
    result = prepare(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""
    private = tmp_path / ".env"
    values = dotenv_values(private)
    assert private.stat().st_mode & 0o777 == 0o600
    credentials = [
        values[key]
        for key in (
            "STORAGE_ACCESS_KEY",
            "STORAGE_SECRET_KEY",
            "STORAGE_APP_ACCESS_KEY",
            "STORAGE_APP_SECRET_KEY",
        )
    ]
    assert all(value and len(value) >= 32 for value in credentials)
    assert len(set(credentials)) == 4
    assert values["EXTERNAL_PROVIDERS"] == values["LLM_PROVIDER"] == "fake"
    assert values["AUTOMATION_KILL_SWITCH"] == "true"
    assert values["OUTBOUND_ENABLED"] == values["SYNTHETIC_VOICE_ENABLED"] == "false"
    assert values["ACO_AGENT_LIVE_GRANT_ID"] == values["ACO_EMAIL_LIVE_CONFIG_PATH"] == ""
    assert "OPENAI_API_KEY" not in values
    target = tmp_path / ".tooling/storage/s3-identities.json"
    assert target.stat().st_mode & 0o777 == 0o600
    document = json.loads(target.read_text())
    assert document["identities"][1]["policyNames"] == ["aco-artifact-data"]
    assert "actions" not in document["identities"][1]
    assert all(value not in target.read_text() for value in credentials if value)
    before = private.read_bytes(), target.read_bytes()
    refused = prepare(tmp_path)
    assert refused.returncode == 2
    assert (private.read_bytes(), target.read_bytes()) == before
    assert all(value not in refused.stdout + refused.stderr for value in credentials if value)


def test_ci_rejects_changed_live_defaults_before_writing_private_files(tmp_path: Path) -> None:
    fixture_root(tmp_path)
    example = tmp_path / ".env.example"
    example.write_text(example.read_text().replace("OUTBOUND_ENABLED=false", "OUTBOUND_ENABLED=true"))
    result = prepare(tmp_path)
    assert result.returncode == 2
    assert "Fresh CI setup refused" in result.stderr
    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / ".tooling").exists()


def test_ci_workflow_prepares_storage_before_compose_and_does_not_print_config(tmp_path: Path) -> None:
    workflow = (ROOT / "scripts/test-local-regression").read_text()
    prepare_index = workflow.index("python3 scripts/prepare-ci.py")
    validate_index = workflow.index("docker compose config --quiet")
    assert prepare_index < validate_index < workflow.index("docker compose pull")
    assert "shell: python" not in workflow
    result, calls = run_local_regression(tmp_path / "success")
    assert result.returncode == 0, result.stderr
    assert (
        calls.index(["python3", "scripts/prepare-ci.py"])
        < calls.index(["docker", "compose", "config", "--quiet"])
        < calls.index(["docker", "compose", "pull"])
    )
    assert [call for call in calls if call[:3] == ["docker", "compose", "config"]] == [
        ["docker", "compose", "config", "--quiet"]
    ]
    for name, failure, existing in (
        ("prepare-refused", "python3 scripts/prepare-ci.py", False),
        ("existing-stack", "", True),
        (
            "docker-unavailable",
            "docker ps --all --quiet --filter label=com.docker.compose.project=aco",
            False,
        ),
    ):
        refused, recorded = run_local_regression(tmp_path / name, fail_at=failure, existing_stack=existing)
        assert refused.returncode != 0
        assert not any(call[:2] == ["docker", "compose"] for call in recorded)
    refused, recorded = run_local_regression(tmp_path / "retained-volume", existing_volume=True)
    assert refused.returncode == 2
    assert not any(call[:2] == ["docker", "compose"] for call in recorded)
    assert ["python3", "scripts/prepare-ci.py"] not in recorded
    refused, recorded = run_local_regression(
        tmp_path / "volume-check-failed",
        fail_at="docker volume ls --quiet --filter label=com.docker.compose.project=aco",
    )
    assert refused.returncode != 0
    assert not any(call[:2] == ["docker", "compose"] for call in recorded)
    for selector in (
        "COMPOSE_PROJECT_NAME",
        "COMPOSE_FILE",
        "COMPOSE_ENV_FILES",
        "COMPOSE_PROFILES",
        "COMPOSE_DISABLE_ENV_FILE",
        "COMPOSE_PATH_SEPARATOR",
    ):
        refused, recorded = run_local_regression(tmp_path / selector, compose_selector=selector)
        assert refused.returncode == 2
        assert recorded == []
        assert not (tmp_path / selector / ".tooling").exists()


def test_ci_portable_build_supplies_actual_public_source_identity(tmp_path: Path) -> None:
    workflow = (ROOT / "scripts/test-local-regression").read_text()
    builds = [line.strip() for line in workflow.splitlines() if line.strip().startswith("docker build ")]
    assert len(builds) == 1
    assert '--build-arg ACO_SOURCE_COMMIT="$(git rev-parse HEAD)"' in builds[0]
    assert "--pull=false" in builds[0] and "-f infra/testing/Dockerfile" in builds[0]
    assert "scripts/test-portable" in workflow  # The fixed contained runner remains the execution gate.
    result, calls = run_local_regression(tmp_path / "success")
    assert result.returncode == 0, result.stderr
    assert [call for call in calls if call[:2] == ["docker", "build"]] == [
        [
            "docker",
            "build",
            "--pull=false",
            "--build-arg",
            "ACO_SOURCE_COMMIT=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "-f",
            "infra/testing/Dockerfile",
            "-t",
            "aco-offline-t02:locked",
            ".",
        ]
    ]
    assert calls.index(["python3", "tests/portable_source_probe.py"]) < calls.index(["test-portable"])
    refused, recorded = run_local_regression(
        tmp_path / "stale", fail_at="python3 tests/portable_source_probe.py"
    )
    assert refused.returncode == 23
    assert ["test-portable"] not in recorded
    assert recorded[-1] == ["docker", "compose", "down"]


def test_ci_hands_only_generated_policy_to_pinned_storage_user_without_widening_mode(tmp_path: Path) -> None:
    workflow = (ROOT / "scripts/test-local-regression").read_text()
    ownership = "sudo chown --no-dereference 1000:1000 -- .tooling/storage/s3-identities.json"
    assert ownership in workflow
    assert (
        workflow.index("docker build ")
        < workflow.index(ownership)
        < workflow.index("docker compose up --pull never --wait -d")
    )
    assert "test ! -L .tooling/storage/s3-identities.json" in workflow
    assert 'test "$(stat -c %u .tooling/storage/s3-identities.json)" = "$(id -u)"' in workflow
    assert 'test "$(stat -c %a .tooling/storage/s3-identities.json)" = 600' in workflow
    assert 'test "$(stat -c %u:%g:%a .tooling/storage/s3-identities.json)" = 1000:1000:600' in workflow
    assert "chmod" not in workflow
    assert "chown -R" not in workflow
    assert workflow.count("sudo chown") == 1
    result, calls = run_local_regression(tmp_path / "success")
    assert result.returncode == 0, result.stderr
    chown = ["sudo", "chown", "--no-dereference", "1000:1000", "--", ".tooling/storage/s3-identities.json"]
    assert [call for call in calls if call[0] == "sudo"] == [chown]
    build = next(call for call in calls if call[:2] == ["docker", "build"])
    assert (
        calls.index(build)
        < calls.index(chown)
        < calls.index(["docker", "compose", "up", "--pull", "never", "--wait", "-d"])
    )
    assert (tmp_path / "success/.tooling/storage/s3-identities.json").stat().st_mode & 0o777 == 0o600
    refused, recorded = run_local_regression(tmp_path / "symlink", bad_policy=True)
    assert refused.returncode != 0
    assert chown not in recorded
    assert not any(call[:3] == ["docker", "compose", "up"] for call in recorded)
    refused, recorded = run_local_regression(tmp_path / "chown-failed", fail_at=" ".join(chown))
    assert refused.returncode == 23
    assert not any(call[:3] == ["docker", "compose", "up"] for call in recorded)
