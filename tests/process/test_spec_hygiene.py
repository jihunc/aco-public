"""Exact canonical definitions and execution honesty, using temporary repository copies."""

import runpy
import shutil
from pathlib import Path
from typing import Any

import pytest

CHECKER: dict[str, Any] = runpy.run_path("scripts/check_spec.py")


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    for name in ("ACO_SPEC.md", "AGENTS.md", "README.md", ".env.example"):
        shutil.copy2(name, tmp_path / name)
    (tmp_path / "config").mkdir()
    for template in Path("config").glob("*.example"):
        shutil.copy2(template, tmp_path / "config" / template.name)
    shutil.copytree("tests/fixtures", tmp_path / "tests/fixtures")
    shutil.copy2("tests/boundary_contracts.py", tmp_path / "tests/boundary_contracts.py")
    return tmp_path


@pytest.mark.parametrize("family", ["AC", "AT", "BI", "BT", "TP", "DOC", "D"])
def test_missing_definition_is_not_satisfied_by_prose_reference(repository: Path, family: str) -> None:
    path = repository / "ACO_SPEC.md"
    text = path.read_text()
    import re

    text = re.sub(rf"^\| {family}-01 \|.*\n", "", text, flags=re.M)
    path.write_text(text + f"\nStill references {family}-01 in prose.\n")
    assert any("definition" in error.lower() for error in CHECKER["check"](repository))


def test_duplicate_canonical_definition_fails_but_references_are_allowed(repository: Path) -> None:
    path = repository / "ACO_SPEC.md"
    text = path.read_text()
    path.write_text(text + "\nAC-01 remains a valid reference.\n")
    assert CHECKER["check"](repository) == []
    text = text.replace("| AC-01 |", "| AC-01 | conflicting criterion |\n| AC-01 |", 1)
    path.write_text(text)
    assert any("definition" in error.lower() for error in CHECKER["check"](repository))


def test_missing_matrix_is_a_reported_error_not_exception(repository: Path) -> None:
    path = repository / "tests/fixtures/verification/traceability.md"
    path.write_text(
        path.read_text().replace("## Individual acceptance-to-task-to-test-to-evidence matrix", "## Removed")
    )
    assert CHECKER["check"](repository)


def test_planned_coverage_cannot_be_claimed_as_executed(repository: Path) -> None:
    result = CHECKER["check_repository"](repository, mode="implemented")
    assert any("NOT_RUN" in error for error in result)


def test_boundary_duplicate_or_unknown_selector_is_rejected(repository: Path) -> None:
    path = repository / "tests/fixtures/verification/traceability.md"
    path.write_text(path.read_text().replace("`domain=contact`", "`domain=does_not_exist`"))
    assert any("boundary" in error.lower() for error in CHECKER["check"](repository))


@pytest.mark.parametrize(
    "markdown",
    [
        "\n[missing](#absent-section)\n",
        "\n[missing][reference]\n\n[reference]: #absent-section\n",
        "\n[missing](tests/fragment.md#absent-section)\n",
        "\n[missing](tests/missing-reference.md)\n",
    ],
)
def test_markdown_references_and_fragments_are_checked(repository: Path, markdown: str) -> None:
    (repository / "tests/fragment.md").write_text("# Actual heading\n")
    path = repository / "ACO_SPEC.md"
    path.write_text(path.read_text() + markdown)
    assert any("Missing" in error and "link" in error for error in CHECKER["check"](repository))


def test_markdown_code_is_not_a_link_but_real_reference_links_are(repository: Path) -> None:
    (repository / "tests/fragment.md").write_text(
        "# A *formatted* heading\n\n# A formatted heading\n\n## Café détail\n"
    )
    path = repository / "ACO_SPEC.md"
    path.write_text(
        path.read_text()
        + "\n[valid][section]\n\n[section]: tests/fragment.md#a-formatted-heading-1\n"
        + "\n[Unicode](tests/fragment.md#caf%C3%A9-d%C3%A9tail)\n"
        + "\n`[example](missing-example.md)`\n\n```md\n[example](missing-block.md)\n```\n"
    )
    assert CHECKER["check"](repository) == []
