"""Catch Node child escape and HTTP transport accidentally making external calls."""

import json
import socket
import subprocess
from pathlib import Path

import httpx
import pytest
import respx


def test_external_egress_fails_in_every_default_process() -> None:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        result = subprocess.run(
            [
                "node",
                "-e",
                """
const net = require('node:net');
const s = net.connect({host:'127.0.0.1',port:Number(process.argv[1])});
s.on('error', e => {console.log(JSON.stringify({code:e.code}));});
s.on('connect', () => {console.log(JSON.stringify({code:'ESCAPED'}));s.destroy();});
""",
                str(server.getsockname()[1]),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        assert json.loads(result.stdout)["code"] in {"EPERM", "EACCES", "EHOSTUNREACH"}


def test_unregistered_http_provider_is_denied_by_maintained_interception() -> None:
    with respx.mock(assert_all_called=False):
        with pytest.raises(respx.models.AllMockedAssertionError):
            httpx.get("https://api.openai.com/v1/responses")


def test_unit_external_egress_denied() -> None:
    # Real native child denial; not merely a configuration constant.
    test_external_egress_fails_in_every_default_process()


def test_component_external_egress_denied() -> None:
    test_unregistered_http_provider_is_denied_by_maintained_interception()


def test_default_gauntlet_cannot_select_live() -> None:
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/process/fixture_live.py",
            "-m",
            "live_provider",
            "--collect-only",
            "-q",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert "Live provider collection is BLOCKED" in result.stderr


def test_default_profile_excludes_live_sentinel_without_blocking_offline_tests() -> None:
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/process/fixture_live.py",
            "tests/component/test_health.py::test_live_and_ready_are_distinct_and_no_worker_is_fabricated",
            "-q",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "1 passed, 1 deselected" in result.stdout


def test_external_httpx_is_denied_without_test_scoped_interception() -> None:
    with pytest.raises(respx.models.AllMockedAssertionError):
        httpx.get("https://synthetic-provider.invalid/forbidden")


def test_shell_entry_denies_node_options_preload_at_startup(tmp_path: "Path") -> None:
    import platform

    from startup_probe import probe_preload

    command = (
        ["scripts/test-e2e", "test", "--list"]
        if platform.system() == "Darwin"
        else ["node", "-e", "setTimeout(()=>{},100)"]
    )
    assert probe_preload(command, tmp_path) == (0, 0)


def test_native_browser_runner_normalizes_color_without_suppressing_warnings(tmp_path: "Path") -> None:
    import os
    import platform
    import subprocess

    if platform.system() != "Darwin":
        pytest.skip("native shell environment regression; portable has its own startup boundary")
    preload = tmp_path / "warning.cjs"
    preload.write_text("process.emitWarning('synthetic warning must remain visible');")
    result = subprocess.run(
        ["scripts/test-e2e", "test", "--list"],
        env={**os.environ, "NO_COLOR": "1", "FORCE_COLOR": "1", "NODE_OPTIONS": f"--require={preload}"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "synthetic warning must remain visible" in result.stderr
    assert "'NO_COLOR' env is ignored" not in result.stderr


def test_darwin_abi_suite_skips_before_its_import_probe_on_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib
    import platform
    import runpy

    # Load ordinary dependencies before replacing only the unsafe platform probe.
    importlib.import_module("psycopg")
    importlib.import_module("playwright.sync_api")
    monkeypatch.setattr(platform, "system", lambda: "Linux")

    def forbidden_probe(*args: object, **kwargs: object) -> None:
        raise AssertionError("Darwin-only import probe executed on Linux")

    monkeypatch.setattr(socket, "socket", forbidden_probe)
    path = Path(__file__).resolve().parents[1] / "integration/test_offline_egress.py"
    with pytest.raises(pytest.skip.Exception, match="Darwin"):
        runpy.run_path(str(path))
