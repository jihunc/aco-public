"""Current clean tracked source identity for explicitly selected live composition."""

import hashlib
import json
import re
import subprocess
from pathlib import Path

STAMP = "aco-build-source.json"


def source_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _inventory(root: Path) -> dict[str, str]:
    paths = [root / name for name in ("pyproject.toml", "uv.lock", "alembic.ini")]
    for name in ("src", "alembic"):
        directory = root / name
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("LIVE_BUILD_SOURCE_REQUIRED")
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise ValueError("LIVE_BUILD_SOURCE_CHANGED")
            if path.is_file():
                paths.append(path)
    result = {}
    for path in sorted(paths):
        if path.is_symlink() or not path.is_file():
            raise ValueError("LIVE_BUILD_SOURCE_REQUIRED")
        result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def write_build_stamp(root: Path, commit: str) -> None:
    """Trusted image BUILD only. Blank default permits fake images, never live identity.

    The build pipeline supplies the reviewed clean commit, not a runtime environment
    claim. Root-owned image layers contain this public file; no Git/private files ship.
    """
    if commit and re.fullmatch(r"[a-f0-9]{40}", commit) is None:
        raise ValueError("LIVE_BUILD_SOURCE_REQUIRED")
    path = root / STAMP
    if path.exists() or path.is_symlink():
        raise ValueError("LIVE_BUILD_SOURCE_ALREADY_PRESENT")
    path.write_text(
        json.dumps({"version": 1, "source_commit": commit or None, "files": _inventory(root)}, sort_keys=True)
    )
    path.chmod(0o444)


def image_source_commit(root: Path) -> str:
    try:
        path = root / STAMP
        if path.is_symlink() or path.stat().st_size > 1_048_576 or path.stat().st_mode & 0o222:
            raise ValueError("LIVE_BUILD_SOURCE_REQUIRED")
        value = json.loads(path.read_bytes())
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "source_commit", "files"}
            or value["version"] != 1
            or not isinstance(value["source_commit"], str)
            or re.fullmatch(r"[a-f0-9]{40}", value["source_commit"]) is None
        ):
            raise ValueError("LIVE_BUILD_SOURCE_REQUIRED")
        if value["files"] != _inventory(root):
            raise ValueError("LIVE_BUILD_SOURCE_CHANGED")
        return str(value["source_commit"])
    except (OSError, TypeError, json.JSONDecodeError):
        raise ValueError("LIVE_BUILD_SOURCE_REQUIRED") from None


def current_source_commit() -> str:
    root = source_root()
    if not (root / ".git").exists():
        return image_source_commit(root)
    current = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True, timeout=5
    ).stdout.strip()
    clean = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", "src", "scripts", "alembic"],
        cwd=root,
        capture_output=True,
        timeout=5,
    )
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "--", "src", "scripts", "alembic"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if clean.returncode != 0 or untracked.stdout.strip():
        raise ValueError("LIVE_SOURCE_CHANGED")
    return current


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Trusted public image source stamp; no runtime activation")
    parser.add_argument("--build-stamp", required=True)
    args = parser.parse_args()
    write_build_stamp(source_root(), args.build_stamp)
