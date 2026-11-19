"""Setup-time inventory and current-workspace correspondence for cached test images."""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT_FILES = (
    "ACO_SPEC.md",
    "README.md",
    "alembic.ini",
    "AGENTS.md",
    ".env.example",
    ".dockerignore",
    ".gitignore",
    "compose.yaml",
    "compose.product.yaml",
    "pyproject.toml",
    "uv.lock",
    "package.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "playwright.config.ts",
    "tsconfig.json",
    "tests/fixtures/security/public-metadata.json",
    "tests/fixtures/verification/traceability.md",
)
ROOT_DIRS = ("alembic", "src", "tests", "scripts", "infra", "web", ".github")
GENERATED_DIRS = {"node_modules", ".next"}


def build_manifest(root: Path) -> dict[str, Any]:
    files = {}
    if any(not (root / name).is_file() for name in ROOT_FILES):
        raise ValueError("Portable required setup input missing; explicit rebuild required")
    if any(not (root / name).is_dir() for name in ROOT_DIRS):
        raise ValueError("Portable required input directory missing; explicit rebuild required")
    paths = [root / name for name in ROOT_FILES]
    for directory in ROOT_DIRS:
        paths.append(root / directory)
        paths.extend((root / directory).rglob("*"))
    for path in sorted(paths):
        relative = path.relative_to(root)
        if any(part in GENERATED_DIRS for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError("Portable source symlinks are unsupported; explicit rebuild required")
        if not path.is_file() or path.suffix in {".pyc", ".pyo", ".tsbuildinfo"}:
            continue
        payload = path.read_bytes()
        if relative.as_posix() == "web/next-env.d.ts":
            # Only Next's generated dev-vs-production route bridge is normalized;
            # the file remains covered and any other content change changes its hash.
            for bridge in (b"routes.d.ts", b"root-params.d.ts"):
                payload = payload.replace(b"./.next/dev/types/" + bridge, b"./.next/types/" + bridge)
        files[relative.as_posix()] = hashlib.sha256(
            payload + str(path.stat().st_mode & 0o111).encode()
        ).hexdigest()
    digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"schema_version": 1, "files": files, "digest": digest}


def verify_manifest(root: Path, embedded: dict[str, Any]) -> None:
    if not isinstance(embedded, dict):
        raise ValueError("Portable input manifest invalid; explicit rebuild required")
    current = build_manifest(root)
    if embedded != current:
        previous = embedded.get("files", {})
        changed = (
            sorted(
                name
                for name in set(current["files"]) | set(previous)
                if current["files"].get(name) != previous.get(name)
            )
            if isinstance(previous, dict)
            else []
        )
        suffix = ": " + ", ".join(changed[:10]) if changed else ""
        raise ValueError(
            "Portable source/dependency correspondence mismatch; explicit rebuild required" + suffix
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["write"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = build_manifest(args.root)
    args.output.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    print("Portable input manifest:", manifest["digest"], "files:", len(manifest["files"]))


if __name__ == "__main__":
    main()
