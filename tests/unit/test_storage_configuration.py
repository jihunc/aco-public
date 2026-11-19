"""Execute real config rendering and settings selection; use synthetic secrets only."""

import json
import os
import subprocess
import sys
from pathlib import Path

from aco.core.settings import FakePort, load_settings, resolve_ports
from aco.integrations.s3.adapter import StorageRuntime


def setup_env() -> dict[str, str]:
    return {
        "STORAGE_BUCKET": "aco-artifacts",
        "STORAGE_ACCESS_KEY": "synthetic-admin-key-000",
        "STORAGE_SECRET_KEY": "synthetic-admin-secret-000",
        "STORAGE_APP_ACCESS_KEY": "synthetic-app-key-000",
        "STORAGE_APP_SECRET_KEY": "synthetic-app-secret-000",
        "STORAGE_SSE_KEK": "ab" * 32,
    }


def test_renderer_atomic_private_policy_preserves_only_environment_references(tmp_path: Path) -> None:
    env = setup_env()
    destination = tmp_path / "identities.json"
    result = subprocess.run(
        [sys.executable, "scripts/render_storage_config.py", "--output", str(destination)],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0 and not result.stdout
    document = json.loads(destination.read_text())
    assert destination.stat().st_mode & 0o777 == 0o600
    app = document["identities"][1]
    assert "actions" not in app
    assert app["policyNames"] == ["aco-artifact-data"]
    policy = json.loads(document["policies"][0]["content"])
    assert policy["Statement"] == [
        {
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
            "Resource": ["arn:aws:s3:::aco-artifacts/tenants/*"],
        },
        {
            "Effect": "Allow",
            "Action": ["s3:GetEncryptionConfiguration", "s3:GetBucketVersioning"],
            "Resource": ["arn:aws:s3:::aco-artifacts"],
        },
    ]
    assert all(
        value not in destination.read_text() for name, value in env.items() if name != "STORAGE_BUCKET"
    )
    before = destination.read_bytes()
    env["STORAGE_APP_SECRET_KEY"] = ""
    result = subprocess.run(
        [sys.executable, "scripts/render_storage_config.py", "--output", str(destination)],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert destination.read_bytes() == before
    assert all(value not in result.stderr for value in env.values() if value)


def test_application_settings_never_fall_back_to_admin_credentials() -> None:
    env = setup_env()
    env["STORAGE_ENDPOINT"] = "http://127.0.0.1:59000"
    env.pop("STORAGE_APP_ACCESS_KEY")
    env.pop("STORAGE_APP_SECRET_KEY")
    assert isinstance(resolve_ports(load_settings(env)).storage, FakePort)


def test_application_settings_select_only_independent_application_identity() -> None:
    env = setup_env()
    env["STORAGE_ENDPOINT"] = "http://127.0.0.1:59000"
    runtime = resolve_ports(load_settings(env)).storage
    assert isinstance(runtime, StorageRuntime)
    assert runtime.configuration["access_key"].get_secret_value() == env["STORAGE_APP_ACCESS_KEY"]
    assert runtime.configuration["secret_key"].get_secret_value() == env["STORAGE_APP_SECRET_KEY"]


def test_renderer_preserves_existing_short_admin_identifier(tmp_path: Path) -> None:
    env = setup_env()
    env["STORAGE_ACCESS_KEY"] = "admin0001"
    env["STORAGE_SECRET_KEY"] = "synthetic-strong-admin-secret-000000000000000"
    destination = tmp_path / "identities.json"
    result = subprocess.run(
        [sys.executable, "scripts/render_storage_config.py", "--output", str(destination)],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert destination.stat().st_mode & 0o777 == 0o600
    document = json.loads(destination.read_text())
    assert document["identities"][0]["credentials"][0]["accessKey"] == "${AWS_ACCESS_KEY_ID}"
    assert env["STORAGE_ACCESS_KEY"] not in destination.read_text()
