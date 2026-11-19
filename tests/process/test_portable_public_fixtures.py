"""Deterministic public inputs survive packaging and bind both source inventories."""

import runpy
from pathlib import Path

import pytest
from pathspec import GitIgnoreSpec

from aco.core.evidence import PUBLIC_VERIFICATION_FIXTURES, verification_source_digest

REQUIRED = ("tests/fixtures/security/public-metadata.json",)


def test_required_public_fixture_reads_survive_build_context() -> None:
    rules = GitIgnoreSpec.from_lines(Path(".dockerignore").read_text().splitlines())
    for name in (*REQUIRED, "tests/fixtures/verification/traceability.md"):
        assert Path(name).is_file() and not rules.match_file(name), name


def test_public_fixture_exceptions_do_not_include_private_or_unreviewed_files() -> None:
    rules = GitIgnoreSpec.from_lines(Path(".dockerignore").read_text().splitlines())
    for name in (
        ".git/config", ".env", ".env.live", "web/.env", "web/.env.local", ".tooling/operator.py",
        "config/development.env", "config/private/provider.json", "web/generated.log",
        "rehearsal.trace", "web/rehearsal.trace", "playwright-report/session.json",
        ".superpowers/private.txt", ".superpowers/sdd/review.json", "generated.log", "private.sqlite3",
    ):
        assert rules.match_file(name), name
    assert not rules.match_file(".env.example")


def test_public_fixture_bytes_bind_both_verification_inventories(tmp_path: Path) -> None:
    manifest = runpy.run_path("scripts/portable_manifest.py")
    assert set(PUBLIC_VERIFICATION_FIXTURES) == set(REQUIRED)
    assert set(REQUIRED) <= set(manifest["ROOT_FILES"])
    assert not any(name.startswith(".superpowers/") for name in manifest["ROOT_FILES"])
    for name in manifest["ROOT_FILES"]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("public synthetic test input\n")
    for name in manifest["ROOT_DIRS"]:
        (tmp_path / name).mkdir(exist_ok=True)
    original = manifest["build_manifest"](tmp_path)
    baseline = verification_source_digest(tmp_path)
    for name in REQUIRED:
        path = tmp_path / name
        initial = path.read_bytes()
        path.write_bytes(initial + b"changed public fixture\n")
        assert verification_source_digest(tmp_path) != baseline, name
        with pytest.raises(ValueError, match="explicit rebuild"):
            manifest["verify_manifest"](tmp_path, original)
        path.unlink()
        with pytest.raises(ValueError, match="required setup input missing"):
            manifest["build_manifest"](tmp_path)
        path.write_bytes(initial)
    for name in (".superpowers/private.txt", ".tooling/operator-config.json", ".env.live"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not a public fixture\n")
    assert verification_source_digest(tmp_path) == baseline
    manifest["verify_manifest"](tmp_path, original)
