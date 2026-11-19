"""Fast runner rejects arbitrary selectors and fails closed before executable checks."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("arguments", [["live"], ["web", "arbitrary-command"], ["--contained", "live"]])
def test_fast_runner_rejects_unknown_lanes_without_starting_tools(arguments: list[str]) -> None:
    result = subprocess.run(
        ["/bin/sh", "scripts/fast-checks", *arguments], cwd=ROOT,
        env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "Usage:" in result.stderr


def fixture_runner(path: Path) -> tuple[Path, Path]:
    (path / "scripts").mkdir()
    shutil.copyfile(ROOT / "scripts/fast-checks", path / "scripts/fast-checks")
    trace = path / "trace.jsonl"
    stub = path / "stub.py"
    stub.write_text(
        "import json,os,pathlib,sys\n"
        "name=pathlib.Path(sys.argv[1]).name\n"
        "args=sys.argv[2:]\n"
        "stage='guard' if name=='python' and args[:1]==['-c'] else name\n"
        "with open(os.environ['TRACE_PATH'],'a') as out:\n"
        " out.write(json.dumps({'stage':stage,'args':args})+'\\n')\n"
        "sys.exit(int(os.environ.get('GUARD_EXIT','0')) if stage=='guard' else 0)\n"
    )
    for name in (".venv/bin/python", ".venv/bin/ruff", ".venv/bin/mypy", "bin/node",
                 "node_modules/.bin/tsc", "web/node_modules/.bin/eslint"):
        binary = path / name
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I -S "{stub}" "$0" "$@"\n')
        binary.chmod(0o700)
    # Contained macOS path needs no service bootstrap. No real runtime is executed.
    (path / "bin/uname").write_text("#!/bin/sh\necho Darwin\n")
    (path / "bin/uname").chmod(0o700)
    (path / "web/tests").mkdir()
    (path / "web/tests/synthetic.test.mjs").write_text("// fixture\n")
    return path, trace


@pytest.mark.parametrize("lane", ["python", "web", "all"])
def test_fast_runner_stops_when_inherited_kernel_policy_is_absent(tmp_path: Path, lane: str) -> None:
    root, trace = fixture_runner(tmp_path)
    result = subprocess.run(
        ["/bin/sh", "scripts/fast-checks", "--contained", lane], cwd=root,
        env={"PATH": str(root / "bin") + ":/usr/bin:/bin", "TRACE_PATH": str(trace), "GUARD_EXIT": "37"},
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 37
    calls = [json.loads(line) for line in trace.read_text().splitlines()]
    assert [call["stage"] for call in calls] == ["guard"]


def test_fast_web_lane_runs_type_lint_build_and_components_after_kernel_guard(tmp_path: Path) -> None:
    root, trace = fixture_runner(tmp_path)
    result = subprocess.run(
        ["/bin/sh", "scripts/fast-checks", "--contained", "web"], cwd=root,
        env={"PATH": str(root / "bin") + os.pathsep + "/usr/bin:/bin", "TRACE_PATH": str(trace)},
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in trace.read_text().splitlines()]
    assert [call["stage"] for call in calls] == [
        "guard", "node", "tsc", "tsc", "eslint", "node", "node", "node"
    ]
    assert calls[1]["args"] == ["web/scripts/generate-api.mjs", "--check"]
    assert calls[5]["args"] == ["node_modules/next/dist/bin/next", "build", "--webpack"]
    assert calls[-1]["args"] == [
        "--test", "web/tests/synthetic.test.mjs", "tests/voice_live_web_fixture.test.mjs"
    ]
