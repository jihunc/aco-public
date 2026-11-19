"""Current requirement selectors must exist; structural coverage awards no execution."""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TRACEABILITY = ROOT / "tests/fixtures/verification/traceability.md"
COUNTS = {"AC": 26, "AT": 20, "BI": 10, "BT": 8, "TP": 10, "DOC": 7}


def assert_current_selector(selector: str) -> None:
    filename, _, function = selector.partition("::")
    path = ROOT / filename
    assert path.is_file(), selector
    assert filename.startswith("tests/") and path.suffix in {".py", ".ts"}, selector
    if path.suffix == ".py":
        functions = {
            node.name
            for node in ast.parse(path.read_text()).body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
        }
        assert functions, selector
        assert not function or function in functions, selector
    else:
        assert "test(" in path.read_text(), selector


def test_current_acceptance_matrix_has_real_pointers_and_keeps_all_original_criteria() -> None:
    text = TRACEABILITY.read_text().split("## Individual acceptance-to-task-to-test-to-evidence matrix", 1)[1]
    rows = [
        line.split("|") for line in text.splitlines() if re.match(r"^\| (AC|AT|BI|BT|TP|DOC)-\d+ \|", line)
    ]
    expected = {f"{family}-{n:02}" for family, count in COUNTS.items() for n in range(1, count + 1)}
    assert len(rows) == len(expected) == 81
    assert {row[1].strip() for row in rows} == expected
    spec = (ROOT / "ACO_SPEC.md").read_text()
    for row in rows:
        requirement = row[1].strip()
        assert re.search(rf"^\| {re.escape(requirement)} \|", spec, re.M), requirement
        assert re.search(r"T\d{2}", row[2]) and row[4].strip(), requirement
        selectors = re.findall(r"`(tests/[^`]+)`", row[3])
        assert selectors or "EXTERNAL GATE" in row[3], requirement
        for selector in selectors:
            assert_current_selector(selector)
