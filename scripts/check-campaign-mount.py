"""Disposable host/container campaign proof. Never opens a real authorization ledger.

Requires an already built product Python image. Creates a NEW temporary synthetic
ledger, uses no network/credentials, leaves receipts private, and retains failures.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4


def client(directory: Path, action: str, marker: str) -> int:
    from aco.integrations.provider_smoke import campaign as module
    from aco.integrations.provider_smoke import source

    # This explicit no-network fixture tests the transaction primitive, not live activation.
    # Normal image clients remain blocked by the mixed-version safety gate.
    source.source_root = lambda: directory
    path = directory / "synthetic.sqlite3"
    # Test-only canonical fixture; no alternate path exists in application code.
    module.canonical_campaign_path = lambda: path
    if action.startswith("legacy-"):
        # Equivalent pre-correction SQLite-only transaction seam. Disposable test only.
        patch.object(module.LiveCampaign, "_db", module.LiveCampaign._locked_db).start()
        action = action.removeprefix("legacy-")
    campaign = module.LiveCampaign(path)
    if action == "create":
        module.LiveCampaign.create(path, authorization_ref=module.AUTHORIZATION_REF)
    elif action == "hold" or action == "crash":
        with campaign._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if action == "crash":
                db.execute("PRAGMA cache_size=1")
                # Force journaled writes without committing or changing immutable rows.
                db.execute("CREATE TABLE crash_probe(payload BLOB)")
                db.execute("INSERT INTO crash_probe VALUES(zeroblob(1048576))")
            try:
                (directory / (marker + "held")).write_text("held")
            except OSError as error:
                print(repr(error), file=sys.stderr, flush=True)
                raise
            while not (directory / (marker + "release")).exists():
                time.sleep(0.02)
    elif action == "allocate":
        allocation = module.CampaignAllocation(
            id=uuid4(), grant_id=uuid4(), provider="openai", source_commit="a" * 40,
            max_usd=Decimal("6"), max_requests=1,
        )
        try:
            campaign.allocate(allocation)
        except module.CampaignBlocked as error:
            print(str(error))
            return 3
    elif action == "locked":
        try:
            with campaign._db() as db:
                db.execute("BEGIN IMMEDIATE")
        except module.CampaignBlocked as error:
            if str(error) in {"CAMPAIGN_LOCKED", "PRIVATE_CAMPAIGN_REQUIRED"}:
                return 0
            raise
        return 4
    elif action == "totals":
        print(json.dumps(campaign.totals()))
    return 0


def proof(image: str, output: Path) -> None:
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    output = output.resolve()
    (output / ".git").mkdir(mode=0o700)
    root = Path(__file__).resolve().parents[1]
    script = output / "probe.py"
    shutil.copyfile(__file__, script)
    script.chmod(0o600)
    local = [sys.executable, str(script), "--client", str(output)]
    container = [
        "docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--user", f"{os.getuid()}:{os.getgid()}",
        "--mount", f"type=bind,src={output},dst={output}", image,
        "python", str(script), "--client", str(output),
    ]
    environment = dict(os.environ, PYTHONPATH=str(root / "src"), PYTHONDONTWRITEBYTECODE="1")
    receipts: dict[str, object] = {"image": image, "status": "FAIL", "steps": []}
    steps: list[str] = []
    receipts["steps"] = steps

    def run(command: list[str], expected: int = 0) -> str:
        result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=40)
        if result.returncode != expected:
            raise RuntimeError(f"Synthetic mount probe failed: expected {expected}, got {result.returncode}")
        return result.stdout.strip()

    release = output / "unused"

    def holder(command: list[str], crash: bool = False) -> subprocess.Popen[bytes]:
        nonlocal release
        marker = uuid4().hex
        held = output / (marker + "held")
        release = output / (marker + "release")
        process = subprocess.Popen(
            command + ["--marker", marker, "crash" if crash else "hold"], env=environment,
                                   stdout=(output / "holder.stdout").open("ab"),
                                   stderr=(output / "holder.stderr").open("ab"))
        deadline = time.monotonic() + 20
        while not held.exists():
            if process.poll() is not None or time.monotonic() > deadline:
                process.kill()
                process.wait()
                raise RuntimeError("Synthetic holder did not acquire transaction")
            time.sleep(0.03)
        return process

    process = None
    try:
        run(local + ["create"])
        for name, first, second in (
            ("host-to-container", local, container),
            ("container-to-host", container, local),
            ("container-to-container", container, container),
        ):
            process = holder(first)
            run(second + ["locked"])
            release.touch()
            if process.wait(timeout=15) != 0:
                raise RuntimeError("Synthetic holder failed")
            process = None
            steps.append(name + "-exclusion")
        # Older preserved operator clients do not honor the new directory lock.
        # Require actual SQLite exclusion too, in BOTH host/container directions.
        for name, first, second in (
            ("new-container-old-host", container, local),
            ("new-host-old-container", local, container),
        ):
            process = holder(first)
            run(second + ["legacy-locked"])
            release.touch()
            if process.wait(timeout=15) != 0:
                raise RuntimeError("Mixed-version synthetic holder failed")
            process = None
            steps.append(name + "-exclusion")
        # Four concurrent complete-ceiling reservations: only three fit USD20.
        def reserve(command: list[str]) -> int:
            return subprocess.run(command + ["allocate"], env=environment, capture_output=True,
                                  timeout=40).returncode
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(reserve, [local, container, local, container]))
        if sorted(results) != [0, 0, 0, 3]:
            raise RuntimeError("Synthetic aggregate exclusion failed")
        before = json.loads(run(container + ["totals"]))
        if before["reserved_usd"] != "18":
            raise RuntimeError("Synthetic retained total failed")
        steps.append("concurrent-host-container-ceilings-retained")
        # Kill a host writer with an uncommitted hot journal. Kernel closes SQLite;
        # cooperative lock deliberately survives. Explicit test-owner reconciliation
        # occurs only after holder termination, then SQLite recovers the SAME file.
        process = holder(local, crash=True)
        process.kill()
        process.wait(timeout=10)
        process = None
        run(container + ["locked"])
        (output / "synthetic.sqlite3.lock" / "owner").unlink()
        (output / "synthetic.sqlite3.lock").rmdir()
        after = json.loads(run(container + ["totals"]))
        if after != before:
            raise RuntimeError("Synthetic journal recovery changed exposure")
        steps.extend(["crash-blocks-without-takeover", "same-file-hot-journal-recovery-retains-exposure"])
        receipts["status"] = "PASS"
    finally:
        if process is not None and process.poll() is None:
            release.touch()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        (output / "receipt.json").write_text(json.dumps(receipts, indent=2) + "\n")
        (output / "receipt.json").chmod(0o600)
    print(json.dumps(receipts))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", type=Path)
    parser.add_argument("--marker", default="")
    parser.add_argument("action", nargs="?")
    parser.add_argument("--image")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.client is not None:
        raise SystemExit(client(args.client, args.action, args.marker))
    if not args.image or args.output is None:
        parser.error("Provide --image and a fresh --output directory")
    proof(args.image, args.output)


if __name__ == "__main__":
    main()
