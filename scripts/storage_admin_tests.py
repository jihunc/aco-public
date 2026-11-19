"""Allow only the audited storage file in the explicit native Docker-admin lane."""

import argparse
import errno
import os
import socket
import subprocess
import sys


def verify_kernel_boundary() -> None:
    """Fail before pytest imports unless real native TCP/UDP/child calls are denied."""
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
            with socket.socket(family, kind) as listener:
                listener.bind((host, 0))
                if kind == socket.SOCK_STREAM:
                    listener.listen(1)
                target = listener.getsockname()
                if target[1] in {55432, 58080, 59000, 58333, 59101}:
                    raise RuntimeError("Safety probe selected an allowed port; rerun before testing")
                with socket.socket(family, kind) as probe:
                    if kind == socket.SOCK_STREAM:
                        if probe.connect_ex(target) != errno.EPERM:
                            raise RuntimeError("Native storage-admin TCP egress denial is absent")
                    else:
                        try:
                            probe.sendto(b"synthetic local guard probe", target)
                        except PermissionError:
                            pass
                        else:
                            raise RuntimeError("Native storage-admin UDP egress denial is absent")
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import socket,errno; s=socket.socket(); "
                "assert s.connect_ex(('127.0.0.1',9)) == errno.EPERM; s.close()"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if child.returncode != 0:
        raise RuntimeError("Storage-admin child did not inherit native egress denial")


def main() -> None:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("-k")
    parser.add_argument("--tb", choices=("short", "long", "line", "no", "native"), default="short")
    parser.add_argument("--maxfail", type=int)
    parser.add_argument("--junitxml")
    options = parser.parse_args()
    verify_kernel_boundary()
    arguments = [
        sys.executable,
        "-m",
        "pytest",
        "--aco-storage-admin",
        "tests/integration/test_storage_infrastructure.py",
        "-q",
        f"--tb={options.tb}",
    ]
    if options.k:
        arguments.extend(("-k", options.k))
    if options.maxfail is not None:
        arguments.append(f"--maxfail={options.maxfail}")
    if options.junitxml:
        arguments.append(f"--junitxml={options.junitxml}")
    os.environ["ACO_RUN_STORAGE_INTEGRATION"] = "1"
    os.execv(sys.executable, arguments)


if __name__ == "__main__":
    main()
