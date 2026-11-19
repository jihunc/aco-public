"""Safe local-listener regressions for the default offline process boundary."""

import ctypes
import errno
import json
import platform
import socket
import struct
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg
import pytest
from playwright.sync_api import Error as BrowserError
from playwright.sync_api import sync_playwright


def _verify_before_collection() -> None:
    # This code executes at module import, before any pytest fixture or test body.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        with socket.socket() as probe:
            assert probe.connect_ex(listener.getsockname()) == errno.EPERM, (
                "Offline native policy must be established before collecting test modules"
            )


# sockaddr layout, native EPERM and sandbox-exec assertions below are Darwin-specific.
# Linux retains conftest startup enforcement plus test_portable_egress.py.
if platform.system() != "Darwin":
    pytest.skip(
        "Darwin native sandbox ABI; Linux uses its separate portable kernel suite", allow_module_level=True
    )

_verify_before_collection()


@pytest.mark.parametrize("method", ["connect", "connect_ex", "sendto", "sendmsg"])
@pytest.mark.parametrize("family", [socket.AF_INET, socket.AF_INET6])
def test_unapproved_local_destination_is_denied(method: str, family: socket.AddressFamily) -> None:
    """Even a listening loopback destination must require an explicitly allowed service port."""
    kind = socket.SOCK_DGRAM if method in ("sendto", "sendmsg") else socket.SOCK_STREAM
    with socket.socket(family, kind) as listener:
        listener.bind(("127.0.0.1" if family == socket.AF_INET else "::1", 0))
        if kind == socket.SOCK_STREAM:
            listener.listen(1)
        destination = listener.getsockname()
        with socket.socket(family, kind) as client:
            if method == "connect_ex":
                assert client.connect_ex(destination) == errno.EPERM
            else:
                with pytest.raises(PermissionError):
                    if method == "connect":
                        client.connect(destination)
                    elif method == "sendto":
                        client.sendto(b"synthetic local probe", destination)
                    else:
                        client.sendmsg([b"synthetic local probe"], [], 0, destination)


def test_native_libc_and_libpq_cannot_bypass_the_boundary() -> None:
    """No Python socket monkeypatch can account for a blocked native libc/libpq call."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        native = ctypes.CDLL(None, use_errno=True)
        native.socket.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_int)
        native.connect.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
        native.close.argtypes = (ctypes.c_int,)
        descriptor = native.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
        assert descriptor >= 0
        address = struct.pack(
            "=BBH4s8s", 16, socket.AF_INET, socket.htons(port), socket.inet_aton("127.0.0.1"), b""
        )
        try:
            assert native.connect(descriptor, address, len(address)) == -1
            assert ctypes.get_errno() == errno.EPERM
        finally:
            native.close(descriptor)
        with pytest.raises(psycopg.OperationalError, match="Operation not permitted"):
            psycopg.connect(host="127.0.0.1", port=port, dbname="synthetic", connect_timeout=1)


def test_python_child_inherits_native_boundary_without_pytest_or_an_environment_flag() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import socket,sys,json; "
                    "s=socket.socket(); "
                    "print(json.dumps({'errno':s.connect_ex(('127.0.0.1',int(sys.argv[1])))})); s.close()"
                ),
                str(listener.getsockname()[1]),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        assert json.loads(child.stdout) == {"errno": errno.EPERM}


def test_default_lane_cannot_delegate_networking_to_docker() -> None:
    """The privileged daemon must not be an implicit escape from the default profile."""
    daemon = Path.home() / ".docker/run/docker.sock"
    if not daemon.exists():
        pytest.skip("Local Docker Desktop socket is absent")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        with pytest.raises(PermissionError):
            client.connect(str(daemon))


def test_default_lane_cannot_promote_a_child_to_storage_admin() -> None:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [str(root / "scripts/test-storage-admin"), "tests/integration/test_dependency_compatibility.py"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode != 0
    assert "Operation not permitted" in result.stderr


class SyntheticHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Synthetic local forbidden destination")

    def log_message(self, format: str, *args: object) -> None:
        pass


def test_actual_browser_is_denied_without_route_interception() -> None:
    """The browser's native network service must inherit the same kernel restriction."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), SyntheticHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=str(chrome) if chrome.exists() else None,
            )
            try:
                page = browser.new_page()
                with pytest.raises(BrowserError, match="ERR_ACCESS_DENIED"):
                    page.goto(f"http://127.0.0.1:{server.server_port}/synthetic", timeout=10000)
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
