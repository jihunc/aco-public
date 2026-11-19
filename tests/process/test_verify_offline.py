"""Full verification is explicit and still enters the maintained kernel-contained runner."""

import runpy
import subprocess
from pathlib import Path
from typing import Any

import pytest


def test_explicit_all_selects_backend_groups_without_live_or_helper_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(str(Path("scripts").resolve()))
    module = runpy.run_path("scripts/verify_offline.py")
    selected = []
    for group in (
        "unit",
        "component",
        "integration",
        "contracts",
        "business_impact",
        "process",
        "evaluations",
    ):
        path = tmp_path / "tests" / group / "test_actual.py"
        path.parent.mkdir(parents=True)
        path.write_text("def test_actual(): pass\n")
        selected.append(path.relative_to(tmp_path).as_posix())
    for name in ("tests/live/test_provider.py", "tests/e2e/test_browser.py", "tests/helper.py"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("raise AssertionError('not an offline backend selector')\n")
    module["verify_offline"].__globals__["check_repository"] = lambda root, mode: []
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def invoke(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 7)

    monkeypatch.setattr(subprocess, "run", invoke)
    monkeypatch.setenv("PYTEST_ADDOPTS", "unsafe option")
    monkeypatch.setenv("PYTEST_PLUGINS", "unapproved_plugin")
    result = module["verify_offline"](tmp_path, [], tmp_path / "report.json", all_tests=True)
    assert result == 7 and len(calls) == 1
    args, options = calls[0]
    assert args[0] == str(tmp_path / "scripts/test-offline")
    assert set(args[1 : 1 + len(selected)]) == set(selected)
    assert args[1 + len(selected) :] == [
        "-q",
        "-m",
        "not live_provider",
        f"--boundary-report={tmp_path / 'report.json'}",
    ]
    assert options["cwd"] == tmp_path
    assert "PYTEST_ADDOPTS" not in options["env"] and "PYTEST_PLUGINS" not in options["env"]
    assert options["env"]["PYTHONPATH"] == f"{tmp_path / 'tests'}:{tmp_path / 'src'}"
    assert module["verify_offline"](tmp_path, selected[:1], tmp_path / "report.json", all_tests=True) == 2
    assert len(calls) == 1
