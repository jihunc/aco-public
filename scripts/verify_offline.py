#!/usr/bin/env python3
"""Run selected maintained offline tests and retain boundary evidence, never a live lane."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from check_spec import COVERAGE, ROOT, check_repository

OFFLINE_GROUPS = (
    "unit",
    "component",
    "integration",
    "contracts",
    "business_impact",
    "process",
    "evaluations",
)


def verify_offline(root: Path, selectors: list[str], report: Path, *, all_tests: bool = False) -> int:
    errors = check_repository(root, "planned")
    if all_tests:
        if selectors:
            errors.append("Choose --all or explicit impacted selectors, never both")
        else:
            selectors = [
                path.relative_to(root).as_posix()
                for group in OFFLINE_GROUPS
                for path in sorted((root / "tests" / group).rglob("test_*.py"))
                if "live" not in path.relative_to(root).parts
            ]
    for selector in selectors:
        path = (root / selector.split("::", 1)[0]).resolve()
        if (
            not path.is_relative_to((root / "tests").resolve())
            or not path.is_file()
            or "live" in path.relative_to(root).parts
            or path.suffix != ".py"
        ):
            errors.append(f"Not an explicit offline Python test selector: {selector}")
    if not selectors:
        errors.append("An explicit impacted test selection is required")
    if errors:
        print("\n".join(f"FAIL: {error}" for error in errors))
        return 2
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_PLUGINS", None)
    env["PYTHONPATH"] = str(root / "tests") + os.pathsep + str(root / "src")
    # The existing native runner applies the kernel egress policy before plugins.
    result = subprocess.run(
        [
            str(root / "scripts/test-offline"),
            *selectors,
            "-q",
            "-m",
            "not live_provider",
            f"--boundary-report={report.resolve()}",
        ],
        cwd=root,
        env=env,
        check=False,
    )
    print("Selected offline execution recorded; unconsumed cases remain NOT_RUN. No acceptance awarded.")
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("selectors", nargs="*")
    parser.add_argument("--all", action="store_true", help="Explicit full offline backend selection")
    parser.add_argument("--report", type=Path, default=ROOT / COVERAGE.parent / "boundary-execution.json")
    args = parser.parse_args()
    return verify_offline(ROOT, args.selectors, args.report, all_tests=args.all)


if __name__ == "__main__":
    sys.exit(main())
