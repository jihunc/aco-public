"""Establish the native offline boundary before importing collected test modules.

scripts/test-offline also applies it before Python/plugin startup. Ordinary pytest
applies the identical inherited kernel policy here or fails closed. There is no
environment-variable bypass and no fixture teardown that can reopen networking.
The Linux/container lane proves its startup firewall and irreversible capability drop separately.
"""

import argparse
import ctypes
import json
import os
import platform
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest_plugins = ["boundary_evidence"]


def enforce_native_offline_boundary() -> None:
    if platform.system() == "Linux":
        # The portable bootstrap installs firewall rules before Python starts, then
        # irrevocably removes all capabilities. An environment flag is no bypass.
        import errno
        import socket

        status = Path("/proc/self/status").read_text()
        if "CapBnd:\t0000000000000000" not in status or "NoNewPrivs:\t1" not in status:
            raise pytest.UsageError("Portable lane requires dropped capabilities and no-new-privileges")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            with socket.socket() as probe:
                if probe.connect_ex(listener.getsockname()) not in {
                    errno.EPERM,
                    errno.EACCES,
                    errno.EHOSTUNREACH,
                }:
                    raise pytest.UsageError("Portable native egress policy is absent")
        return
    if platform.system() != "Darwin":
        raise pytest.UsageError("No verified offline kernel policy for this platform")
    root = Path(__file__).resolve().parents[1]
    profile = root / "scripts" / "offline-macos.sb"
    if "--aco-storage-admin" in sys.argv:
        # This is an explicit privileged capability, never an environment bypass.
        # Reject unrelated selectors/options before collecting their Python modules.
        parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
        parser.add_argument("--aco-storage-admin", action="store_true")
        parser.add_argument(
            "selector",
            choices=[
                "tests/integration/test_storage_infrastructure.py",
                str(root / "tests/integration/test_storage_infrastructure.py"),
            ],
        )
        parser.add_argument("-q", action="store_true")
        parser.add_argument("-k")
        parser.add_argument("--tb", choices=("short", "long", "line", "no", "native"))
        parser.add_argument("--maxfail", type=int)
        parser.add_argument("--junitxml")
        parser.parse_args(sys.argv[1:])
        if os.environ.get("PYTEST_ADDOPTS") or os.environ.get("PYTEST_PLUGINS"):
            raise pytest.UsageError("Storage-admin runner forbids injected pytest selectors/plugins")
        profile = root / "scripts" / "storage-admin-macos.sb"
    profile_text = profile.read_text().replace(
        '(param "DOCKER_SOCKET")',
        json.dumps(str(Path.home() / ".docker/run/docker.sock")),
    )
    try:
        sandbox = ctypes.CDLL("/usr/lib/system/libsystem_sandbox.dylib")
        sandbox.sandbox_init.argtypes = (
            ctypes.c_char_p,
            ctypes.c_uint64,
            ctypes.POINTER(ctypes.c_char_p),
        )
        sandbox.sandbox_init.restype = ctypes.c_int
        sandbox.sandbox_free_error.argtypes = (ctypes.c_char_p,)
        error = ctypes.c_char_p()
        result = sandbox.sandbox_init(profile_text.encode(), 0, ctypes.byref(error))
        if result != 0:
            detail = error.value.decode() if error.value else "native initialization failed"
            if error.value:
                sandbox.sandbox_free_error(error)
            raise pytest.UsageError(f"Offline network policy unavailable: {detail}")
    except (OSError, AttributeError) as error:
        raise pytest.UsageError("Native offline network policy cannot be established") from error


enforce_native_offline_boundary()

# An inherited operator selection is never an offline-test default. Explicit
# synthetic private-configuration tests install their own selectors after startup.
for _selector in (
    "ACO_AGENT_LIVE_CONFIG_PATH",
    "ACO_AGENT_LIVE_GRANT_ID",
    "ACO_EMAIL_LIVE_CONFIG_PATH",
    "ACO_VOICE_LIVE_CONFIG_PATH",
    "ACO_LIVE_AUTHORIZATION_REF",
    "ACO_LIVE_CAMPAIGN_FILE",
    "ACO_LIVE_ALLOCATION_ID",
):
    os.environ.pop(_selector, None)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--aco-storage-admin", action="store_true", help="explicit audited local Docker-admin lane"
    )


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if any(item.get_closest_marker("live_provider") for item in items):
        raise pytest.UsageError("Live provider collection is BLOCKED: use separately authorized live runner")


@pytest.fixture(autouse=True)
def intercept_unexpected_httpx() -> "Iterator[None]":
    """Maintained HTTP interception plus inherited kernel policy for all other IO."""
    import re
    from urllib.parse import urlsplit

    import respx

    allowed = {("127.0.0.1", port) for port in (55432, 58080, 59000, 58333, 53000, 58000)}
    allowed.update(("localhost", port) for port in (58080, 59000, 53000, 58000))
    for name in ("ACO_TEST_STORAGE_ENDPOINT", "ACO_TEST_OIDC_DISCOVERY_URL"):
        parsed = urlsplit(os.environ.get(name, ""))
        if parsed.hostname and parsed.port:
            allowed.add((parsed.hostname, parsed.port))
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        for host, port in allowed:
            router.route(url__regex=rf"^http://{re.escape(host)}:{port}(?:/|$)").pass_through()
        yield
