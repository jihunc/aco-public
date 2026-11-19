"""Native Linux proof: firewall applies to libc/libpq/children/Node/Chrome/DNS.

No route interception substitutes for browser containment. The synthetic DNS name
is blocked before Docker resolver DNAT, never forwarded externally.
"""

import ctypes
import errno
import json
import os
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

pytestmark = pytest.mark.skipif(platform.system() != "Linux", reason="Executed by scripts/test-portable")
DENIED = {errno.EPERM, errno.EACCES, errno.EHOSTUNREACH}


def test_native_libc_libpq_and_child_process_are_denied() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        native = ctypes.CDLL(None, use_errno=True)
        native.socket.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_int)
        native.connect.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
        native.close.argtypes = (ctypes.c_int,)
        descriptor = native.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
        address = struct.pack(
            "=HH4s8s", socket.AF_INET, socket.htons(port), socket.inet_aton("127.0.0.1"), b""
        )
        try:
            assert native.connect(descriptor, address, len(address)) == -1
            assert ctypes.get_errno() in DENIED
        finally:
            native.close(descriptor)
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(host="127.0.0.1", port=port, dbname="synthetic", connect_timeout=1)
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import socket,json,sys;"
                    "print(json.dumps(socket.socket().connect_ex(('127.0.0.1',int(sys.argv[1])))))"
                ),
                str(port),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert json.loads(child.stdout) in DENIED


@pytest.mark.parametrize("family,host", [(socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")])
def test_udp_packets_cannot_reach_even_local_unapproved_listener(
    family: socket.AddressFamily, host: str
) -> None:
    with socket.socket(family, socket.SOCK_DGRAM) as listener:
        listener.bind((host, 0))
        listener.settimeout(0.1)
        with socket.socket(family, socket.SOCK_DGRAM) as client:
            try:
                client.sendto(b"synthetic probe", listener.getsockname())
            except OSError as error:
                assert error.errno in DENIED
        with pytest.raises(TimeoutError):
            listener.recvfrom(1024)


def test_embedded_dns_rewritten_ports_and_libc_resolution_are_denied() -> None:
    # All embedded-resolver addresses/ports are dropped in raw OUTPUT before NAT.
    for port in (53, 35555):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
            listener.bind(("127.0.0.11", port))
            listener.settimeout(0.1)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                try:
                    client.sendto(b"synthetic DNS probe", ("127.0.0.11", port))
                except OSError as error:
                    assert error.errno in DENIED
            with pytest.raises(TimeoutError):
                listener.recvfrom(1024)
    child = subprocess.run(
        [sys.executable, "-c", "import socket;socket.getaddrinfo('aco-egress-probe.invalid',443)"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert child.returncode != 0
    assert "gaierror" in child.stderr


def test_cannot_reacquire_firewall_authority_raw_socket_or_docker_ipc() -> None:
    status = Path("/proc/self/status").read_text()
    assert "CapBnd:\t0000000000000000" in status
    assert "NoNewPrivs:\t1" in status
    result = subprocess.run(["iptables", "-F"], capture_output=True, text=True)
    assert result.returncode != 0
    with pytest.raises(PermissionError):
        socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
    assert not Path("/var/run/docker.sock").exists()
    result = subprocess.run(["setpriv", "--reuid=0", "id"], capture_output=True, text=True)
    assert result.returncode != 0


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"unexpected escape")

    def log_message(self, format: str, *args: object) -> None:
        pass


def test_actual_chromium_is_denied_without_request_interception() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path="/usr/bin/chromium", headless=True, args=["--no-sandbox"]
            )
            try:
                page = browser.new_page()
                permitted = page.goto(os.environ["ACO_TEST_OIDC_DISCOVERY_URL"], timeout=5000)
                assert permitted is not None and permitted.ok
                with pytest.raises(BrowserError, match=r"net::ERR_"):
                    page.goto(f"http://127.0.0.1:{server.server_port}", timeout=5000)
                assert browser.is_connected() and not page.is_closed()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
