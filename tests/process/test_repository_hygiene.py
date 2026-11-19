"""Keep development output and private state out of the product snapshot."""

import subprocess
from pathlib import Path


def test_product_tracks_no_development_output_or_private_state() -> None:
    paths = subprocess.run(
        ["git", "ls-files", "-z"], check=True, capture_output=True, text=True
    ).stdout.split("\0")
    forbidden_parts = {".superpowers", ".tooling", "node_modules", "test-results", "playwright-report"}
    for name in filter(None, paths):
        # A deleted indexed file will not enter the cleaned commit.
        if not Path(name).exists():
            continue
        path = Path(name)
        assert not forbidden_parts.intersection(path.parts), name
        assert path.suffix not in {".log", ".sqlite3", ".dump", ".bak", ".pyc"}, name
        assert not (path.name.startswith(".env") and path.name != ".env.example"), name
