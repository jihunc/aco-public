"""Exercise actual planning checks against corrupt copies; no text-presence success substitute."""

import runpy
import shutil
from pathlib import Path

import pytest

check = runpy.run_path("scripts/check_spec.py")["check"]


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("id", "Canonical acceptance IDs missing/unknown"),
        ("mapping", "Traceability requires exactly one individual mapping per acceptance ID"),
        ("env", "Typed environment names missing from .env.example"),
        ("link", "Missing relative link"),
    ],
)
def test_planning_checker_rejects_structural_drift(tmp_path: Path, mutation: str, expected: str) -> None:
    for name in ("ACO_SPEC.md", "AGENTS.md", "README.md", ".env.example"):
        shutil.copy2(name, tmp_path / name)
    (tmp_path / "config").mkdir()
    for template in Path("config").glob("*.example"):
        shutil.copy2(template, tmp_path / "config" / template.name)
    shutil.copytree("tests/fixtures", tmp_path / "tests/fixtures")
    shutil.copy2("tests/boundary_contracts.py", tmp_path / "tests/boundary_contracts.py")
    assert check(tmp_path) == []
    if mutation == "env":
        path = tmp_path / ".env.example"
        path.write_text(path.read_text().replace("EXTERNAL_PROVIDERS=fake\n", ""))
    elif mutation == "mapping":
        path = tmp_path / "tests/fixtures/verification/traceability.md"
        path.write_text(path.read_text() + "\n| TP-01 | duplicate mapping |\n")
    else:
        path = tmp_path / "ACO_SPEC.md"
        path.write_text(path.read_text() + ("\nAC-99\n" if mutation == "id" else "\n[broken](missing.md)\n"))
    assert any(expected in error for error in check(tmp_path))


def test_milestone_report_is_emitted_but_unexecuted_boundaries_fail(tmp_path: Path) -> None:
    import json
    import subprocess
    import sys

    report = tmp_path / "boundary-execution.json"
    report.write_text(json.dumps({"cases": [{"id": "not-executed", "status": "NOT_RUN"}]}))
    result = subprocess.run(
        [sys.executable, "scripts/check_spec.py", "--mode", "planned", "--boundary-report", str(report)],
        text=True,
        capture_output=True,
    )
    assert "BOUNDARY_EXECUTION:" in result.stdout, result.stderr
    assert '"status": "NOT_RUN"' in result.stdout
    assert result.returncode != 0
    assert "FAIL:" in result.stdout
