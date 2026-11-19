"""Exercise fixed diagnostic commands through disposable executable doubles, never Docker."""

import json
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values
from process.test_ci_preparation import fixture_root, prepare, run_local_regression

ROOT = Path(__file__).resolve().parents[2]


def test_ci_failure_diagnostics_redact_generated_controls_and_keep_error(tmp_path: Path) -> None:
    fixture_root(tmp_path)
    assert prepare(tmp_path).returncode == 0
    values = dotenv_values(tmp_path / ".env")
    controls = [value for key, value in values.items() if key.startswith("STORAGE_") and value]
    controls = [value for value in controls if len(value) == 64]
    assert len(controls) == 5
    binary = tmp_path / "bin"
    binary.mkdir()
    calls = tmp_path / "calls.jsonl"
    fake = binary / "docker"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json,sys,pathlib\n"
        f"calls=pathlib.Path({str(calls)!r})\n"
        "with calls.open('a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n"
        "if sys.argv[1:]==['compose','ps','--all','-q','storage-core']:\n"
        " print('a'*64)\n"
        "else:\n"
        f" print({(' '.join(controls) + ' ' + controls[0].upper())!r})\n"
        " print('FATAL permission denied /run/aco/s3-identities.json',file=sys.stderr)\n"
        " print('x'*20000)\n"
        " sys.exit(1)\n"
    )
    fake.chmod(0o700)
    for name in ("df", "free"):
        (binary / name).symlink_to(fake)
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/ci-storage-diagnostics.py")],
        cwd=tmp_path,
        env={"PATH": str(binary)},
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert all(value not in result.stdout for value in controls)
    assert controls[0].upper() not in result.stdout
    assert "[REDACTED_HEX]" in result.stdout
    assert "FATAL permission denied /run/aco/s3-identities.json" in result.stdout
    assert "[OUTPUT_TRUNCATED]" in result.stdout
    assert len(result.stdout) < 85000
    commands = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(commands) == 6
    assert any(command[:3] == ["compose", "logs", "--no-color"] for command in commands)
    assert any(command[0] == "inspect" and command[-1] == "a" * 64 for command in commands)
    assert ["-h", "/"] in commands and ["-m"] in commands
    assert all("config" not in command and "Env" not in " ".join(command) for command in commands)


def test_ci_diagnostics_run_only_for_infrastructure_failure_before_cleanup(tmp_path: Path) -> None:
    up = ["docker", "compose", "up", "--pull", "never", "--wait", "-d"]
    diagnose = ["python3", "scripts/ci-storage-diagnostics.py"]
    for name, failure in (
        ("success", ""),
        ("infrastructure", " ".join(up)),
        ("source", "python3 tests/portable_source_probe.py"),
        ("tests", "test-portable"),
        ("cleanup", "docker compose down"),
    ):
        result, calls = run_local_regression(tmp_path / name, fail_at=failure)
        assert result.returncode == (23 if failure else 0), result.stderr
        assert calls[-1] == ["docker", "compose", "down"]
        if name == "infrastructure":
            assert calls.index(up) < calls.index(diagnose) < len(calls) - 1
            assert ["python3", "tests/portable_source_probe.py"] not in calls
            assert ["test-portable"] not in calls
        else:
            assert diagnose not in calls
