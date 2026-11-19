"""Explicit reviewed change-impact completeness, never semantic or executed acceptance."""

import ast
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

TRACEABILITY_FILE = "tests/fixtures/verification/traceability.md"


@dataclass(frozen=True)
class ChangeImpact:
    contract_sections: tuple[str, ...]
    implementation_paths: tuple[str, ...]
    test_paths: tuple[str, ...]
    schema_paths: tuple[str, ...] = ()
    migration_paths: tuple[str, ...] = ()


def section_body(text: str, number: str) -> str:
    headings = list(re.finditer(r"^(#{2,6})\s+(\d+(?:\.\d+)*)(?:\.\s|\s)[^\n]*\n", text, re.M))
    for index, heading in enumerate(headings):
        if heading[2] == number:
            end = next(
                (later.start() for later in headings[index + 1 :] if len(later[1]) <= len(heading[1])),
                len(text),
            )
            return text[heading.end() : end].strip()
    return ""


def meaningful(text: str, path: str) -> str:
    if path.endswith(".py"):
        return ast.dump(ast.parse(text), include_attributes=False)
    return text.strip()


def check_change(root: Path, base: str, impact: ChangeImpact) -> list[str]:
    """Compare staged/worktree artifacts to one pinned local commit, with no checkout or execution.

    The reviewer supplies impacted files/clauses. This verifies that declared
    obligations changed; it cannot infer semantic consistency or run migrations.
    """
    if (
        not impact.contract_sections
        or any(not re.fullmatch(r"12\.\d+(?:\.\d+)*", number) for number in impact.contract_sections)
        or not impact.implementation_paths
        or not impact.test_paths
        or bool(impact.schema_paths) != bool(impact.migration_paths)
    ):
        return ["Incomplete reviewed change impact (contracts/implementation/tests/schema+migration)"]
    root = root.resolve()
    try:
        commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "--end-of-options", base + "^{commit}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        names = (
            subprocess.run(
                ["git", "-C", str(root), "diff", "--name-only", "-z", commit, "--"],
                check=True,
                capture_output=True,
            )
            .stdout.decode()
            .split("\0")
        )

        def previous(name: str) -> str:
            result = subprocess.run(
                ["git", "-C", str(root), "show", f"{commit}:{name}"],
                capture_output=True,
                text=True,
            )
            return result.stdout if result.returncode == 0 else ""

        errors = []
        required = (
            TRACEABILITY_FILE,
            *impact.implementation_paths,
            *impact.test_paths,
            *impact.schema_paths,
            *impact.migration_paths,
        )
        for name in required:
            path = (root / name).resolve()
            if not path.is_relative_to(root) or name not in names or not path.is_file():
                errors.append(f"Missing same-change artifact: {name}")
            elif meaningful(path.read_text(), name) == meaningful(previous(name), name):
                errors.append(f"No substantive artifact change: {name}")
        for name in impact.test_paths:
            if not name.startswith("tests/") or not name.endswith(".py"):
                errors.append(f"Expected executable Python test artifact: {name}")
        for name in impact.migration_paths:
            if not name.startswith("alembic/versions/") or not name.endswith(".py"):
                errors.append(f"Expected canonical migration artifact: {name}")
        spec = (root / "ACO_SPEC.md").read_text()
        old_spec = previous("ACO_SPEC.md")
        for number in ("11", *impact.contract_sections):
            before, after = section_body(old_spec, number), section_body(spec, number)
            if not before or not after or before == after:
                errors.append(f"Missing same-change canonical section {number}")
        return errors
    except (OSError, ValueError, SyntaxError, subprocess.SubprocessError) as exc:
        return [f"Cannot verify reviewed change: {type(exc).__name__}"]
