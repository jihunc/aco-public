"""Actual contract artifacts on a disposable Git branch; structural checks do not award acceptance."""

import re
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aco.core.spec_change import ChangeImpact

import pytest

TRACEABILITY = "tests/fixtures/verification/traceability.md"
DTO = "src/aco/features/usage/dto.py"
SERVICE = "src/aco/features/usage/service.py"
TEST = "tests/integration/test_usage_native_subject.py"
MIGRATION = "alembic/versions/r0026_email_rfc_identities.py"
MODEL = "src/aco/features/communications/models.py"


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def insert_section(text: str, number: str, note: str) -> str:
    return re.sub(
        rf"^(#+ {re.escape(number)}(?:[. ]|$)[^\n]*\n)", rf"\g<1>\n{note}\n", text, count=1, flags=re.M
    )


@pytest.fixture
def rehearsal(tmp_path: Path) -> tuple[Path, str]:
    root = Path(__file__).resolve().parents[2]
    for name in ("ACO_SPEC.md", TRACEABILITY, DTO, SERVICE, MODEL):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, target)
    model = tmp_path / MODEL
    model.write_text(model.read_text().split("\nclass CommunicationRfcIdentity(", 1)[0])
    # Rehearse the actual additive native-subject contract against its legacy DTO.
    dto = tmp_path / DTO
    dto.write_text(
        "\n".join(line for line in dto.read_text().splitlines() if "native_subject:" not in line) + "\n"
    )
    service = tmp_path / SERVICE
    service.write_text(
        service.read_text().replace("native_subject=event.native_subject,", "native_subject=None,")
    )
    git(tmp_path, "init", "-q", "-b", "verification-rehearsal")
    git(tmp_path, "add", ".")
    git(
        tmp_path,
        "-c",
        "user.name=ACO offline rehearsal",
        "-c",
        "user.email=offline@example.test",
        "commit",
        "-qm",
        "retained public baseline",
    )
    base = git(tmp_path, "rev-parse", "HEAD")
    shutil.copyfile(root / DTO, dto)
    service = tmp_path / SERVICE
    shutil.copyfile(root / SERVICE, service)
    test = tmp_path / TEST
    test.parent.mkdir(parents=True)
    shutil.copyfile(root / TEST, test)
    spec = tmp_path / "ACO_SPEC.md"
    text = spec.read_text()
    for number, note in (
        (
            "11",
            "T28 isolated rehearsal: reviewed internal native-subject identity; "
            "no grant or financial authority. DOC-01 DOC-07.",
        ),
        (
            "12.5",
            "T28 rehearsal: native_subject is an immutable insert/replay identity; "
            "legacy null has no new revision key. DOC-03.",
        ),
    ):
        text = insert_section(text, number, note)
    spec.write_text(text)
    traceability = tmp_path / TRACEABILITY
    traceability.write_text(
        traceability.read_text() + "\nT28 isolated rehearsal: run actual test_usage_native_subject; "
        "runtime acceptance remains separately recorded.\n"
    )
    git(tmp_path, "add", ".")
    return tmp_path, base


def impact() -> "ChangeImpact":
    from aco.core.spec_change import ChangeImpact

    return ChangeImpact(contract_sections=("12.5",), implementation_paths=(DTO, SERVICE), test_paths=(TEST,))


def test_actual_contract_changeset_and_each_omitted_artifact(rehearsal: tuple[Path, str]) -> None:
    from aco.core.spec_change import check_change

    root, base = rehearsal
    selected = impact()
    assert check_change(root, base, selected) == []
    for name in (DTO, SERVICE, TEST, TRACEABILITY):
        path = root / name
        after = path.read_bytes()
        old = subprocess.run(["git", "-C", str(root), "show", f"{base}:{name}"], capture_output=True)
        if old.returncode == 0:
            path.write_bytes(old.stdout)
        else:
            path.unlink()
        assert any(name in error for error in check_change(root, base, selected))
        path.write_bytes(after)
    assert check_change(root, base, selected) == []


@pytest.mark.parametrize("number", ["11", "12.5"])
def test_one_omitted_owning_section_fails_even_when_spec_file_changed(
    rehearsal: tuple[Path, str], number: str
) -> None:
    from aco.core.spec_change import check_change, section_body

    root, base = rehearsal
    path = root / "ACO_SPEC.md"
    original = git(root, "show", f"{base}:ACO_SPEC.md")
    current = path.read_text()
    path.write_text(current.replace(section_body(current, number), section_body(original, number)))
    assert any(f"section {number}" in error for error in check_change(root, base, impact()))


def test_real_migration_model_requirement_cannot_be_satisfied_by_comment_or_missing_file(
    rehearsal: tuple[Path, str],
) -> None:
    from aco.core.spec_change import ChangeImpact, check_change

    root, base = rehearsal
    selected = ChangeImpact(
        contract_sections=("12.5",),
        implementation_paths=(DTO, SERVICE),
        test_paths=(TEST,),
        schema_paths=(MODEL,),
        migration_paths=(MIGRATION,),
    )
    assert any(MIGRATION in error for error in check_change(root, base, selected))
    migration = root / MIGRATION
    migration.parent.mkdir(parents=True)
    shutil.copyfile(Path(__file__).resolve().parents[2] / MIGRATION, migration)
    model = root / MODEL
    model.write_text(model.read_text() + "\n# not a schema change\n")
    git(root, "add", ".")
    assert any(MODEL in error for error in check_change(root, base, selected))
    # Restore the actual mapped RFC table and its matching real r26 migration.
    # Structural completeness alone still does not claim a migrated database.
    shutil.copyfile(Path(__file__).resolve().parents[2] / MODEL, model)
    assert check_change(root, base, selected) == []
    migration.unlink()
    assert any(MIGRATION in error for error in check_change(root, base, selected))


def test_public_checker_requires_declared_change_and_local_base(
    rehearsal: tuple[Path, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import runpy
    import sys

    root, base = rehearsal
    namespace = runpy.run_path("scripts/check_spec.py")
    main = namespace["main"]
    main.__globals__["ROOT"] = root
    main.__globals__["check_repository"] = lambda root, mode: []
    argv = [
        "check_spec.py",
        "--mode",
        "planned",
        "--contract-section",
        "12.5",
        "--implementation-path",
        DTO,
        "--implementation-path",
        SERVICE,
        "--test-path",
        TEST,
    ]
    monkeypatch.setattr(sys, "argv", argv)
    assert main() == 1
    assert "require --change-base" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", argv + ["--change-base", base])
    assert main() == 0
    assert "acceptance NOT_RUN" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", argv + ["--change-base", "nonexistent"])
    assert main() == 1
