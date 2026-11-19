"""Runtime assertions cannot omit keys, use another consumer or silently duplicate cases."""

import json
from pathlib import Path
from typing import Any

import pytest
from boundary_contracts import boundary_inventory
from boundary_evidence import BoundaryObserver


def observer() -> BoundaryObserver:
    return BoundaryObserver(
        "tests/process/test_example.py::test_actual",
        [
            {"id": "example", "expected": {"effect_count": 0, "state": "BLOCKED"}},
        ],
        {"example": "tests/process/test_example.py::test_actual"},
    )


@pytest.mark.parametrize("actual", [{"effect_count": 0}, {"effect_count": 1, "state": "BLOCKED"}])
def test_executed_boundary_consumers_assert_every_mandatory_output(actual: dict[str, object]) -> None:
    recorder = observer()
    with pytest.raises(AssertionError):
        recorder.assert_observed("example", actual)
    assert recorder.observed == {}


def test_wrong_consumer_and_duplicate_are_not_evidence() -> None:
    recorder = observer()
    recorder.nodeid = "tests/process/test_wrong.py::test_wrong"
    with pytest.raises(AssertionError, match="Wrong boundary consumer"):
        recorder.assert_observed("example", {"effect_count": 0, "state": "BLOCKED"})
    recorder.nodeid = recorder.owners["example"]
    recorder.assert_observed("example", {"effect_count": 0, "state": "BLOCKED"})
    with pytest.raises(AssertionError, match="Duplicate"):
        recorder.assert_observed("example", {"effect_count": 0, "state": "BLOCKED"})


def test_every_boundary_case_has_exactly_one_planned_consumer() -> None:
    inventory = boundary_inventory()
    cases = json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]
    assigned = [case for group in inventory.values() for case in group["case_ids"]]
    assert len(assigned) == len(set(assigned)) == len(cases)
    assert set(assigned) == {case["id"] for case in cases}


def test_report_rejects_unexecuted_echo_and_stale_source(tmp_path: Path) -> None:
    import runpy

    checker = runpy.run_path("scripts/check_spec.py")
    validate = checker["validate_boundary_execution"]
    from hashlib import sha256

    cases = json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]
    report: dict[str, Any] = {
        "schema_version": 1,
        "lane": "offline",
        "acceptance_awarded": False,
        "pytest_exitstatus": 0,
        "spec_sha256": sha256(Path("ACO_SPEC.md").read_bytes()).hexdigest(),
        "fixture_sha256": sha256(Path("tests/fixtures/policies/boundaries.json").read_bytes()).hexdigest(),
        "registry_sha256": sha256(Path("tests/boundary_contracts.py").read_bytes()).hexdigest(),
        "cases": [{"id": case["id"], "status": "NOT_RUN"} for case in cases],
    }
    # This is a negative fixture only, never accepted application evidence.
    report["cases"][0].update(status="PASS", actual={})
    errors = validate(Path.cwd(), report)
    assert any("outputs" in error or "execution" in error for error in errors)
    report["cases"][0]["status"] = "NOT_RUN"
    report["spec_sha256"] = "0" * 64
    assert any("stale" in error.lower() for error in validate(Path.cwd(), report))


def test_valid_report_is_consistent_but_source_edit_invalidates_it(tmp_path: Path) -> None:
    import runpy
    from hashlib import sha256

    from aco.core.evidence import verification_source_digest

    checker = runpy.run_path("scripts/check_spec.py")
    files = {
        "ACO_SPEC.md": "Temporary parser test fixture, no product acceptance.\n",
        "tests/fixtures/policies/boundaries.json": json.dumps(
            {
                "cases": [
                    {"id": "example", "domain": "example", "expected": {"count": 0}},
                ]
            }
        ),
        "tests/boundary_contracts.py": "# temporary fixture registry\n",
        "tests/test_example.py": "def test_actual():\n    assert 0 == 0\n",
        str(checker["TRACEABILITY"]): (
            "| example | `domain=example` | T28 | `tests/test_example.py::test_actual` |\n"
        ),
        "src/example.py": "VALUE = 0\n",
    }
    for name, value in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    report = {
        "schema_version": 1,
        "lane": "offline",
        "acceptance_awarded": False,
        "pytest_exitstatus": 0,
        "source_sha256": verification_source_digest(tmp_path),
        **{
            key: sha256((tmp_path / name).read_bytes()).hexdigest()
            for key, name in (
                ("spec_sha256", "ACO_SPEC.md"),
                ("registry_sha256", "tests/boundary_contracts.py"),
                ("fixture_sha256", "tests/fixtures/policies/boundaries.json"),
                ("traceability_sha256", str(checker["TRACEABILITY"])),
            )
        },
        "cases": [
            {
                "id": "example",
                "test": "tests/test_example.py::test_actual",
                "nodeid": "tests/test_example.py::test_actual",
                "status": "PASS",
                "actual": {"count": 0},
                "test_sha256": sha256((tmp_path / "tests/test_example.py").read_bytes()).hexdigest(),
            }
        ],
    }
    report["source_unchanged"] = True
    report["end_snapshot"] = {
        key: report[key]
        for key in (
            "source_sha256", "spec_sha256", "fixture_sha256", "registry_sha256", "traceability_sha256"
        )
    }
    assert checker["validate_boundary_execution"](tmp_path, report) == []
    (tmp_path / "src/example.py").write_text("VALUE = 1\n")
    assert checker["validate_boundary_execution"](tmp_path, report) == [
        "Boundary report stale executable source"
    ]


def test_dependency_and_build_changes_invalidate_execution_digest(tmp_path: Path) -> None:
    from aco.core.evidence import verification_source_digest

    baseline = verification_source_digest(tmp_path)
    for name in (
        "uv.lock",
        "pyproject.toml",
        "pnpm-lock.yaml",
        "web/package.json",
        "web/tsconfig.json",
        "compose.product.yaml",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("version-one\n")
        first = verification_source_digest(tmp_path)
        assert first != baseline, name
        path.write_text("version-two\n")
        assert verification_source_digest(tmp_path) != first, name
        path.unlink()
    (tmp_path / ".env.live").write_text("PRIVATE=not-read\n")
    assert verification_source_digest(tmp_path) == baseline


def test_malformed_boundary_report_is_a_structured_failure() -> None:
    import runpy

    validate = runpy.run_path("scripts/check_spec.py")["validate_boundary_execution"]
    malformed_reports: list[Any] = [[], {"cases": [None]}, {"cases": "invalid"}]
    for malformed in malformed_reports:
        errors = validate(Path.cwd(), malformed)
        assert errors and any("Malformed" in error for error in errors)


def test_maximum_evidence_records_actual_zero_without_fabricating_one() -> None:
    recorder = BoundaryObserver(
        "tests/test_example.py::test_actual",
        [{"id": "limit", "expected": {"provider_submissions_max": 1}}],
        {"limit": "tests/test_example.py::test_actual"},
    )
    recorder.assert_observed("limit", {"provider_submissions_max": 0})
    assert recorder.observed["limit"] == {"provider_submissions_max": 0}
    for invalid in (2, -1, True, "0"):
        fresh = BoundaryObserver(recorder.nodeid, list(recorder.cases.values()), recorder.owners)
        with pytest.raises(AssertionError):
            fresh.assert_observed("limit", {"provider_submissions_max": invalid})


@pytest.mark.parametrize(
    "changed", ["src/value.py", "ACO_SPEC.md", "tests/fixtures/verification/traceability.md"]
)
def test_in_run_source_changes_cannot_publish_current_passing_report(tmp_path: Path, changed: str) -> None:
    import os
    import subprocess
    import sys

    root = Path.cwd()
    files = {
        "ACO_SPEC.md": "# Synthetic temporary spec\n",
        "tests/fixtures/verification/traceability.md": "# Synthetic traceability\n",
        "src/value.py": "VALUE = 1\n",
        "tests/fixtures/policies/boundaries.json": json.dumps(
            {"cases": [{"id": "sample", "expected": {"count": 0}}]}
        ),
        "boundary_contracts.py": (
            "def boundary_inventory():\n"
            "    return {'sample': {'test': 'tests/test_sample.py::test_actual', 'case_ids': ['sample']}}\n"
        ),
        "tests/boundary_contracts.py": "# synthetic registry bytes\n",
        "tests/test_sample.py": (
            "from pathlib import Path\n"
            "def test_actual(boundary_observer):\n"
            "    boundary_observer.assert_observed('sample', {'count': 0})\n"
            f"    p = Path({changed!r})\n"
            "    p.write_text(p.read_text() + '\\n# changed during execution\\n')\n"
        ),
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(map(str, [tmp_path, root / "tests", root / "src"]))
    report = tmp_path / "report.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            "/dev/null",
            "--rootdir",
            str(tmp_path),
            "-p",
            "boundary_evidence",
            "tests/test_sample.py",
            "-q",
            f"--boundary-report={report}",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    actual = json.loads(report.read_text())
    assert completed.returncode != 0, completed.stdout
    assert actual["source_unchanged"] is False
    assert actual["cases"][0]["status"] == "FAIL"
