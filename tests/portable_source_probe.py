"""Explicit trusted-host regression: stale source/locks must never launch cached tests.

Run after the explicit image build. Uses only Docker metadata/no-network image
inspection; temporary input trees contain no .env or service credentials.
"""

import runpy
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    api = runpy.run_path(str(ROOT / "scripts/portable_manifest.py"))
    sys.path.insert(0, str(ROOT / "scripts"))
    runner = runpy.run_path(str(ROOT / "scripts/test-portable"))
    image, embedded = runner["cached_image_manifest"]()
    api["verify_manifest"](ROOT, embedded)
    current = api["build_manifest"](ROOT)
    print("PASS: actual cached image matches unmodified baseline", image, embedded["digest"])
    for changed in ("src/aco/api/app.py", "uv.lock", "pnpm-lock.yaml", "compose.product.yaml"):
        with tempfile.TemporaryDirectory() as directory:
            replica = Path(directory)
            for name in current["files"]:
                target = replica / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / name, target)
            assert api["build_manifest"](replica) == current
            target = replica / changed
            target.write_text(target.read_text() + "\n# synthetic correspondence mutation\n")
            result = subprocess.run(
                [sys.executable, str(replica / "scripts/test-portable")],
                cwd=replica,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 2, changed
            assert "source/dependency correspondence" in result.stderr, changed
            assert "explicit rebuild required" in result.stderr, changed
            assert "Portable verified image:" not in result.stdout, changed
            print("PASS: actual cached-image runner rejected changed", changed, "before test startup")


if __name__ == "__main__":
    main()
