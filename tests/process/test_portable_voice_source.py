"""Faithful Gitless stdlib probe of the fixed portable image's public source stamp."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from aco.integrations.provider_smoke.source import current_source_commit

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "infra/testing/Dockerfile"
MODULE = "aco.integrations.provider_smoke.source"


def gitless_tree(target: Path) -> None:
    for name in ("src", "alembic"):
        # Match the existing Docker context exclusion; do not filter the verifier.
        shutil.copytree(ROOT / name, target / name, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("pyproject.toml", "uv.lock", "alembic.ini"):
        shutil.copyfile(ROOT / name, target / name)
    actual = Path("src/aco/integrations/provider_smoke/source.py")
    assert (target / actual).read_bytes() == (ROOT / actual).read_bytes()
    assert not (target / ".git").exists()


def command(root: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.defpath, "PYTHONPATH": str(root / "src")}
    if "ENV PYTHONDONTWRITEBYTECODE=1" in DOCKERFILE.read_text():
        env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, *arguments], cwd=root, env=env, text=True, capture_output=True, timeout=10
    )


def probe(root: Path) -> subprocess.CompletedProcess[str]:
    return command(
        root, ["-c", f"from {MODULE} import current_source_commit; print(current_source_commit())"]
    )


@pytest.mark.parametrize("explicit_commit", [True, False])
def test_gitless_portable_build_uses_existing_stamp_without_inventory_churn(
    tmp_path: Path, explicit_commit: bool
) -> None:
    gitless_tree(tmp_path)
    initial = probe(tmp_path)
    assert initial.returncode != 0 and "LIVE_BUILD_SOURCE_REQUIRED" in initial.stderr
    commit = current_source_commit()
    dockerfile = DOCKERFILE.read_text()
    if f"-m {MODULE} --build-stamp" in dockerfile:
        stamped = command(tmp_path, ["-m", MODULE, "--build-stamp", commit if explicit_commit else ""])
        assert stamped.returncode == 0, stamped.stderr
    observed = probe(tmp_path)
    if not explicit_commit:
        assert observed.returncode != 0 and "LIVE_BUILD_SOURCE_REQUIRED" in observed.stderr
        return
    assert observed.returncode == 0, observed.stderr
    assert observed.stdout.strip() == commit
    assert probe(tmp_path).stdout.strip() == commit  # Repeated runtime imports must not change inventory.
    assert not list((tmp_path / "src").rglob("*.pyc"))
    assert (tmp_path / "aco-build-source.json").stat().st_mode & 0o222 == 0
    changed = tmp_path / "src/aco/__init__.py"
    changed.write_text(changed.read_text() + "\n# source drift must remain refused\n")
    drift = probe(tmp_path)
    assert drift.returncode != 0 and "LIVE_BUILD_SOURCE_CHANGED" in drift.stderr


def test_portable_stamp_order_and_blank_build_default_do_not_enable_provider_execution() -> None:
    dockerfile = DOCKERFILE.read_text()
    assert 'ARG ACO_SOURCE_COMMIT=""' in dockerfile
    assert dockerfile.index("ENV PYTHONDONTWRITEBYTECODE=1") < dockerfile.index(
        "COPY --chown=10001:10001 . ."
    )
    assert dockerfile.index('--build-stamp "$ACO_SOURCE_COMMIT"') < dockerfile.index(
        "scripts/portable_manifest.py write"
    )
    assert "ENV ACO_SOURCE_COMMIT" not in dockerfile
    assert "COPY .git" not in dockerfile and "COPY .env" not in dockerfile


def test_portable_voice_runtime_directory_owns_private_exclusive_receipts(tmp_path: Path) -> None:
    # uv uses .tooling/uv-cache during the root-owned build, before runtime UID10001.
    # Native setup runs uv as the runtime user, so it does not expose this mismatch.
    directory = tmp_path / ".tooling"
    directory.mkdir(mode=0o500)
    receipt = directory / "voice-live-browser-runtime.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        if os.geteuid() != 0:
            with pytest.raises(PermissionError):
                os.open(receipt, flags, 0o600)
    finally:
        directory.chmod(0o700)
    dockerfile = DOCKERFILE.read_text()
    assert "cache-dir = \".tooling/uv-cache\"" in (ROOT / "pyproject.toml").read_text()
    assert "uv sync --frozen && chown 10001:10001 /aco /aco/.tooling" in dockerfile
    assert "chmod 0700 /aco/.tooling" in dockerfile
    descriptor = os.open(receipt, flags, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write("explicit nonsecret runtime receipt")
    assert receipt.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        os.open(receipt, flags, 0o600)
    receipt.unlink()
    target = tmp_path / "unrelated-retained-file"
    target.write_text("unchanged")
    receipt.symlink_to(target)
    with pytest.raises(FileExistsError):
        os.open(receipt, flags, 0o600)
    assert target.read_text() == "unchanged"
