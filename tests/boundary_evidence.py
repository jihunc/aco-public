"""Opt-in pytest evidence from actual registered boundary assertions, never source-name scans."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from boundary_contracts import boundary_inventory

from aco.core.evidence import boundary_outputs_match, verification_source_digest
from aco.core.spec_change import TRACEABILITY_FILE


class BoundaryObserver:
    def __init__(self, nodeid: str, cases: list[dict[str, Any]], owners: dict[str, str]):
        self.nodeid = nodeid
        self.cases = {row["id"]: row for row in cases}
        self.owners = owners
        self.observed: dict[str, dict[str, Any]] = {}

    def assert_observed(self, case_id: str, actual: Mapping[str, Any]) -> None:
        assert case_id in self.cases, f"Unknown boundary case {case_id}"
        assert self.nodeid.split("[", 1)[0] == self.owners[case_id], "Wrong boundary consumer"
        assert case_id not in self.observed, f"Duplicate boundary consumption {case_id}"
        expected = self.cases[case_id]["expected"]
        assert set(actual) == set(expected), f"Missing/extra mandatory outputs for {case_id}"
        assert boundary_outputs_match(dict(actual), expected), (case_id, dict(actual), expected)
        # JSON roundtrip freezes values now, before mutable ORM/result objects change.
        self.observed[case_id] = json.loads(json.dumps(dict(actual), allow_nan=False))


OBSERVERS = pytest.StashKey[list[BoundaryObserver]]()
REPORTS = pytest.StashKey[dict[str, list[pytest.TestReport]]]()
INITIAL = pytest.StashKey[dict[str, str]]()
ROWS = pytest.StashKey[dict[str, dict[str, Any]]]()
SNAPSHOT_FILES = {
    "spec_sha256": "ACO_SPEC.md",
    "fixture_sha256": "tests/fixtures/policies/boundaries.json",
    "registry_sha256": "tests/boundary_contracts.py",
    "traceability_sha256": TRACEABILITY_FILE,
}


def snapshot(root: Path) -> dict[str, str]:
    return {"source_sha256": verification_source_digest(root)} | {
        key: digest(root / name) for key, name in SNAPSHOT_FILES.items()
    }


def pytest_sessionstart(session: pytest.Session) -> None:
    if session.config.getoption("--boundary-report"):
        session.config.stash[INITIAL] = snapshot(Path(str(session.config.rootpath)))
        session.config.stash[ROWS] = {
            case: {"id": case, "test": owner["test"], "status": "NOT_RUN", "actual": None}
            for owner in boundary_inventory().values()
            for case in owner["case_ids"]
        }


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--boundary-report", help="Write source-bound observed boundary results (no acceptance award)"
    )


def pytest_configure(config: pytest.Config) -> None:
    config.stash[OBSERVERS] = []
    config.stash[REPORTS] = {}


@pytest.fixture
def boundary_observer(request: pytest.FixtureRequest) -> BoundaryObserver:
    root = Path(str(request.config.rootpath))
    cases = json.loads((root / "tests/fixtures/policies/boundaries.json").read_text())["cases"]
    owners = {case: row["test"] for row in boundary_inventory().values() for case in row["case_ids"]}
    observer = BoundaryObserver(request.node.nodeid, cases, owners)
    request.config.stash[OBSERVERS].append(observer)
    return observer


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Any:
    outcome = yield
    report = outcome.get_result()
    item.config.stash[REPORTS].setdefault(item.nodeid, []).append(report)


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    target = session.config.getoption("--boundary-report")
    if not target:
        return
    root = Path(str(session.config.rootpath))
    initial = session.config.stash[INITIAL]
    try:
        final = snapshot(root)
    except OSError:
        final = None
    unchanged = initial == final
    if not unchanged and exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        exitstatus = int(session.exitstatus)
    rows = session.config.stash[ROWS]
    for observer in session.config.stash[OBSERVERS]:
        reports = session.config.stash[REPORTS].get(observer.nodeid, [])
        passed = len(reports) == 3 and all(report.passed for report in reports)
        for case, actual in observer.observed.items():
            row = rows[case]
            duplicate = row["status"] != "NOT_RUN"
            row.update(
                {
                    "status": "PASS" if passed and not duplicate and unchanged else "FAIL",
                    "actual": actual,
                    "nodeid": observer.nodeid,
                    "test_sha256": digest(root / observer.nodeid.split("::", 1)[0]),
                }
            )
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                **initial,
                "end_snapshot": final,
                "source_unchanged": unchanged,
                "schema_version": 1,
                "lane": "offline",
                "acceptance_awarded": False,
                "recorded_at": datetime.now(UTC).isoformat(),
                "pytest_exitstatus": exitstatus,
                "cases": list(rows.values()),
            },
            indent=2,
        )
        + "\n"
    )
