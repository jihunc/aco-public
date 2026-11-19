#!/usr/bin/env python3
"""Requirement/traceability/evidence consistency; never awards semantic or live acceptance."""

import argparse
import ast
import json
import re
import sqlite3
from collections import Counter
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from aco.core.spec_change import TRACEABILITY_FILE

ROOT = Path(__file__).resolve().parents[1]
TRACEABILITY = Path(TRACEABILITY_FILE)
COVERAGE = Path(".tooling/verification/coverage.json")
COUNTS = {"AC": 26, "AT": 20, "BI": 10, "BT": 8, "TP": 10, "DOC": 7}
EXPECTED = {f"{prefix}-{number:02}" for prefix, count in COUNTS.items() for number in range(1, count + 1)}
DECISIONS = {f"D-{number:02}" for number in range(1, 26)}
ID = r"(?:AC|AT|BI|BT|TP|DOC|D)-\d{2}"
# Summary definitions and normative elaborations both remain mandatory. Different
# wording is not automatically a contradiction; that requires independent review.
DEFINITION_SECTIONS = (
    ("AC", "## 9.", "### 9.1"),
    ("AT", "### 9.3", "### 9.4"),
    ("AT", "#### 1. Functional contract matrix", "#### 1.1 Business impact"),
    ("BI", "## 7.", "## 8."),
    ("BI", "### 12.7", "### 12.8"),
    ("BT", "### 9.4", "## 10."),
    ("BT", "#### 1.1 Business impact", "#### 1.2 Testing isolation"),
    ("TP", "### 8.1", "### 8.2"),
    ("DOC", "#### 7. Documentation-change acceptance IDs", "## 13."),
    ("D", "## 11.", "## 12."),
)


def section(text: str, start: str, end: str) -> str:
    match = re.search(rf"^{re.escape(start)}[^\n]*\n(.*?)(?=^{re.escape(end)})", text, re.M | re.S)
    return match[1] if match else ""


def definitions(spec: str) -> list[str]:
    errors = []
    known = EXPECTED | DECISIONS
    if set(re.findall(rf"\b({ID})\b", spec)) != known:
        errors.append("Canonical acceptance IDs missing/unknown (81 acceptance / 25 decisions)")
    located = Counter[str]()
    for family, start, end in DEFINITION_SECTIONS:
        expected = DECISIONS if family == "D" else {key for key in EXPECTED if key.startswith(family + "-")}
        rows = re.findall(rf"^\| ({family}-\d{{2}}) \|", section(spec, start, end), re.M)
        if Counter(rows) != Counter(expected):
            errors.append(f"Missing/duplicate definition in {start}: {family}")
        located.update(rows)
    all_rows = Counter(re.findall(rf"^\| ({ID}) \|", spec, re.M))
    if all_rows != located:
        errors.append("Requirement definition outside its owning canonical table")
    return errors


def boundary_mapping(root: Path, traceability: str) -> list[str]:
    errors = []
    fixture = root / "tests/fixtures/policies/boundaries.json"
    registry = root / "tests/boundary_contracts.py"
    if not fixture.exists() or not registry.exists():
        return ["Missing boundary fixture/consumer registry"]
    cases = json.loads(fixture.read_text())["cases"]
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)):
        errors.append("Duplicate boundary case IDs")
    parsed = ast.parse(registry.read_text())
    assignments = [
        node.value
        for node in parsed.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "CONSUMERS" for target in node.targets)
    ]
    if len(assignments) != 1:
        return errors + ["Missing/duplicate boundary registry"]
    consumers = ast.literal_eval(assignments[0])
    rows = re.findall(
        r"^\| (\w+) \| `(domain=[^`]+|ids=[^`]+)` \| (T\d{2}) \| `([^`]+)` \|", traceability, re.M
    )
    if Counter(rows) != Counter(tuple(row) for row in consumers):
        errors.append("Traceability boundary selector/test/task differs from registered consumer")
    selected: list[str] = []
    for name, selector, _task, _test in consumers:
        kind, value = selector.split("=", 1)
        requested = value.split(",")
        matched = [
            case["id"]
            for case in cases
            if (case["domain"] == value if kind == "domain" else case["id"] in requested)
        ]
        if not matched or kind not in {"domain", "ids"} or (kind == "ids" and set(matched) != set(requested)):
            errors.append(f"Empty/unknown boundary selector: {name}")
        selected.extend(matched)
    if Counter(selected) != Counter(ids):
        errors.append("Each boundary case needs exactly one consumer")
    return errors


def execution_coverage(root: Path) -> list[str]:
    """Do not turn planned selectors or prose logs into executed acceptance."""
    path = root / COVERAGE
    if not path.exists():
        return ["Missing generated execution coverage; acceptance NOT_RUN"]
    coverage = json.loads(path.read_text())
    rows = coverage.get("acceptance", [])
    errors = []
    if Counter(row.get("id") for row in rows) != Counter(EXPECTED):
        errors.append("Execution coverage requires exactly 81 known acceptance rows")
    for row in rows:
        key = row.get("id")
        if row.get("status") != "PASS":
            errors.append(f"{key}: acceptance {row.get('status', 'NOT_RUN')}; not implemented PASS")
            continue
        # Existing coverage is a planning/report index, not an execution ledger.
        # A PASS needs an independently reviewed canonical ledger record. Text
        # paths and partial checkpoint prose never qualify automatically.
        pointers = row.get("evidence", [])
        if not pointers:
            errors.append(f"{key}: PASS without executed evidence")
        for pointer in pointers:
            if not isinstance(pointer, dict) or set(pointer) != {"ledger", "verification_id"}:
                errors.append(f"{key}: evidence needs canonical ledger/verification_id")
                continue
            ledger = local_file(root, pointer["ledger"])
            if ledger is None or not ledger.is_file():
                errors.append(f"{key}: missing local evidence ledger")
                continue
            from aco.core.evidence import VerificationRow, read_verifications

            records = read_verifications(ledger)
            matched = [record for record in records if record.get("id") == pointer["verification_id"]]
            if len(matched) != 1:
                errors.append(f"{key}: missing unique execution record")
                continue
            record = matched[0]
            verified = VerificationRow.model_validate(
                {k: record[k] for k in VerificationRow.model_fields if k in record}
            )
            if (
                verified.result != "PASS"
                or key not in verified.requirements
                or record.get("latest_critic_outcome") != "PASS"
                or record.get("residual_blockers")
                or record.get("defect_refs")
            ):
                errors.append(f"{key}: execution lacks reviewed passing scope")
    report = root / COVERAGE.parent / "boundary-execution.json"
    if not report.exists():
        errors.append("Boundary executed-consumption/source binding NOT_RUN")
    else:
        errors.extend(validate_boundary_execution(root, json.loads(report.read_text())))
    return errors


def validate_boundary_execution(root: Path, report: dict[str, Any]) -> list[str]:
    """Validate actual pytest output and source binding; semantic independence needs review."""
    from aco.core.evidence import boundary_outputs_match, verification_source_digest

    if (
        not isinstance(report, dict)
        or not isinstance(report.get("cases"), list)
        or any(not isinstance(row, dict) for row in report["cases"])
        or any(not isinstance(row.get("id"), str) for row in report["cases"])
        or any(
            row.get("status") == "PASS" and not isinstance(row.get("nodeid"), str) for row in report["cases"]
        )
    ):
        return ["Malformed boundary execution report"]
    errors: list[str] = []
    snapshot_fields = (
        "source_sha256", "spec_sha256", "fixture_sha256", "registry_sha256", "traceability_sha256"
    )
    if report.get("source_unchanged") is not True or report.get("end_snapshot") != {
        key: report.get(key) for key in snapshot_fields
    }:
        errors.append("Boundary report source changed during execution or initial snapshot missing")
    if report.get("source_sha256") != verification_source_digest(root):
        errors.append("Boundary report stale executable source")
    for field, name in (
        ("spec_sha256", "ACO_SPEC.md"),
        ("fixture_sha256", "tests/fixtures/policies/boundaries.json"),
        ("registry_sha256", "tests/boundary_contracts.py"),
        ("traceability_sha256", str(TRACEABILITY)),
    ):
        if report.get(field) != sha256((root / name).read_bytes()).hexdigest():
            errors.append(f"Boundary report stale source: {name}")
    if (
        report.get("schema_version") != 1
        or report.get("lane") != "offline"
        or report.get("acceptance_awarded") is not False
        or report.get("pytest_exitstatus") != 0
    ):
        errors.append("Boundary report is not a successful offline execution; no acceptance award")
    cases = json.loads((root / "tests/fixtures/policies/boundaries.json").read_text())["cases"]
    owners = dict()
    traceability = (root / TRACEABILITY).read_text()
    for selector, test in re.findall(
        r"^\| \w+ \| `(domain=[^`]+|ids=[^`]+)` \| T\d{2} \| `([^`]+)` \|", traceability, re.M
    ):
        kind, value = selector.split("=", 1)
        for case in cases:
            if case["domain"] == value if kind == "domain" else case["id"] in value.split(","):
                owners[case["id"]] = test
    rows = report.get("cases", [])
    if Counter(row.get("id") for row in rows) != Counter(case["id"] for case in cases):
        errors.append("Boundary execution requires exactly one result for every case")
    expected = {case["id"]: case["expected"] for case in cases}
    for row in rows:
        key = row.get("id")
        if row.get("status") != "PASS":
            errors.append(f"Boundary {key}: {row.get('status', 'NOT_RUN')}")
            continue
        selector = row.get("nodeid", "").split("[", 1)[0]
        if selector != owners.get(key) or row.get("test") != selector:
            errors.append(f"Boundary {key}: wrong/missing executed consumer")
        path = local_file(root, selector.split("::", 1)[0])
        if not path or not path.is_file() or row.get("test_sha256") != sha256(path.read_bytes()).hexdigest():
            errors.append(f"Boundary {key}: stale/missing executed test source")
        if key not in expected or not boundary_outputs_match(row.get("actual"), expected[key]):
            errors.append(f"Boundary {key}: missing/incorrect mandatory outputs")
    return errors


def local_file(root: Path, target: str) -> Path | None:
    path = (root / target).resolve()
    return path if path.is_relative_to(root.resolve()) else None


def check_repository(root: Path, mode: Literal["planned", "implemented"] = "planned") -> list[str]:
    errors: list[str] = []
    if mode not in {"planned", "implemented"}:
        return ["Unknown specification-check mode"]
    try:
        spec = (root / "ACO_SPEC.md").read_text()
        text = (root / TRACEABILITY).read_text()
        errors.extend(definitions(spec))
        heading = "## Individual acceptance-to-task-to-test-to-evidence matrix"
        if heading not in text:
            errors.append("Missing acceptance matrix heading")
        else:
            rows = re.findall(r"^\| ((?:AC|AT|BI|BT|TP|DOC)-\d{2}) \|", text.split(heading, 1)[1], re.M)
            if Counter(rows) != Counter(EXPECTED):
                errors.append("Traceability requires exactly one individual mapping per acceptance ID")
        errors.extend(boundary_mapping(root, text))
        from aco.core.spec_markdown import check_markdown_links

        documents = [root / "ACO_SPEC.md", root / "AGENTS.md", root / TRACEABILITY]
        if (root / "README.md").exists():
            documents.append(root / "README.md")
        errors.extend(check_markdown_links(root, documents))
        from aco.core.settings import ENV_FIELDS

        example = (root / ".env.example").read_text()
        configured = set(re.findall(r"^([A-Z_][A-Z0-9_]*)=", example, re.M))
        if not set(ENV_FIELDS) <= configured:
            errors.append("Typed environment names missing from .env.example")
        if not all(name in spec for name in ENV_FIELDS):
            errors.append("Typed environment names missing from canonical specification")
        if mode == "implemented":
            errors.extend(execution_coverage(root))
    except (OSError, ValueError, KeyError, TypeError, SyntaxError, sqlite3.Error) as exc:
        errors.append(f"Malformed/missing specification input: {type(exc).__name__}")
    return errors


def check(root: Path) -> list[str]:
    return check_repository(root)


def main() -> int:
    parser = argparse.ArgumentParser()
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--planning", action="store_true", help="Compatibility alias for --mode planned")
    selection.add_argument("--mode", choices=("planned", "implemented"))
    parser.add_argument("--change-base", help="Pinned local base for explicit reviewed change completeness")
    for name in ("contract-section", "implementation-path", "test-path", "schema-path", "migration-path"):
        parser.add_argument("--" + name, action="append", default=[])
    parser.add_argument(
        "--boundary-report", type=Path, help="Emit and validate an actual offline execution report"
    )
    args = parser.parse_args()
    errors = check_repository(ROOT, args.mode or "planned")
    if args.boundary_report:
        try:
            report = json.loads(args.boundary_report.read_text())
        except (OSError, ValueError):
            errors.append("Boundary execution report missing or unreadable")
        else:
            print("BOUNDARY_EXECUTION: " + json.dumps(report, sort_keys=True), flush=True)
            errors.extend(validate_boundary_execution(ROOT, report))
    declared = (
        args.contract_section,
        args.implementation_path,
        args.test_path,
        args.schema_path,
        args.migration_path,
    )
    if args.change_base:
        from aco.core.spec_change import ChangeImpact, check_change

        errors.extend(
            check_change(ROOT, args.change_base, ChangeImpact(*(tuple(value) for value in declared)))
        )
    elif any(declared):
        errors.append("Reviewed change paths require --change-base")
    for error in errors:
        print(f"FAIL: {error}")
    if not errors:
        print(
            "PASS: planned consistency; 81 acceptance / 25 decisions. Execution/semantic acceptance NOT_RUN."
        )
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
