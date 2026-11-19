"""Safe listening-target startup proof; standalone mode starts outside Node/pytest.

The explicit standalone command is run outside an inherited test sandbox, so its
positive control can detect an unsafe package-manager parent before the wrapper.
Only synthetic loopback traffic is used; no external addresses are contacted.
"""

import json
import os
import socket
import subprocess
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PERMITTED_LOCAL_PORTS = {53000, 58000, 55432, 58080, 59000, 58333}


def forbidden_listener() -> socket.socket:
    # Ephemeral allocation can return one of the explicitly permitted services.
    # Never mistake legitimate access to such a port for a containment escape.
    for _ in range(64):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        if listener.getsockname()[1] not in PERMITTED_LOCAL_PORTS:
            return listener
        listener.close()
    raise RuntimeError("Unable to allocate a forbidden listening target")


def probe_preload(command: list[str], directory: Path) -> tuple[int, int]:
    with forbidden_listener() as listener:
        listener.listen()
        listener.settimeout(0.1)
        accepted: list[int] = []
        stop = threading.Event()

        def serve() -> None:
            while not stop.is_set():
                try:
                    connection, _ = listener.accept()
                    accepted.append(1)
                    connection.close()
                except TimeoutError:
                    pass

        thread = threading.Thread(target=serve)
        thread.start()
        preload = directory / "preload.cjs"
        preload.write_text(
            "if(!process.env.ACO_STARTUP_PROBE_VISITED){"
            "process.env.ACO_STARTUP_PROBE_VISITED='1';"
            "const s=require('node:net').connect({host:'127.0.0.1',port:"
            + str(listener.getsockname()[1])
            + "});s.on('error',()=>{});s.on('connect',()=>s.destroy());}"
        )
        try:
            result = subprocess.run(
                command,
                cwd=ROOT,
                env={**os.environ, "NODE_OPTIONS": f"--require={preload}"},
                capture_output=True,
                text=True,
                timeout=30,
            )
        finally:
            stop.set()
            thread.join()
        return result.returncode, len(accepted)


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        control = probe_preload(["node", "-e", "setTimeout(()=>{},100)"], root)
        assert control == (0, 1), "Standalone positive control requires uncontained Python parent"
        scripts = json.loads((ROOT / "package.json").read_text())["scripts"]
        entry = (
            ["pnpm", "test:e2e", "--list"]
            if "test:e2e" in scripts
            else ["scripts/test-e2e", "test", "--list"]
        )
        observed = probe_preload(entry, root)
        print("uncontained positive control:", control, "documented entry:", entry, "observed:", observed)
        assert observed == (0, 0), "Documented startup entry delivered to forbidden listening target"


if __name__ == "__main__":
    main()
