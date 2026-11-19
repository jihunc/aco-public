"""Maintained scanner uses tracked synthetic files only and cannot verify keys online."""

import subprocess
from pathlib import Path
from typing import Any

import pytest


def repository(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)


def stage(path: Path, *names: str) -> None:
    subprocess.run(["git", "-C", str(path), "add", "--", *names], check=True, capture_output=True)


@pytest.mark.parametrize("filename", ["app.py", "uv.lock", "style.css"])
def test_real_scanner_detects_synthetic_key_without_network_or_value_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    import requests

    from aco.core.secret_scan import scan_tracked_secrets

    def refused(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Secret scanning must never verify credentials online")

    monkeypatch.setattr(requests.sessions.Session, "request", refused)
    repository(tmp_path)
    marker = "-----BEGIN " + "RSA PRIVATE KEY-----"
    (tmp_path / filename).write_text(f"PRIVATE = {marker!r}\n")
    stage(tmp_path, filename)
    findings = scan_tracked_secrets(tmp_path)
    assert findings and any(f.kind == "Private Key" and f.path == filename for f in findings)
    assert marker not in repr(findings)
    (tmp_path / filename).write_text("PUBLIC = 'ordinary application value'\n")
    assert scan_tracked_secrets(tmp_path) == []


def test_private_untracked_files_are_never_read_and_tracked_private_names_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from detect_secrets.core.secrets_collection import SecretsCollection

    from aco.core.secret_scan import scan_tracked_secrets

    repository(tmp_path)
    (tmp_path / "app.py").write_text("PUBLIC = 1\n")
    private = tmp_path / ".env.live"
    private.write_text("PRIVATE=synthetic-not-a-real-credential\n")
    stage(tmp_path, "app.py")
    observed: list[str] = []
    original = SecretsCollection.scan_file

    def capture(self: SecretsCollection, filename: str) -> None:
        observed.append(filename)
        assert Path(filename).name != ".env.live"
        original(self, filename)

    monkeypatch.setattr(SecretsCollection, "scan_file", capture)
    assert scan_tracked_secrets(tmp_path) == [] and observed
    stage(tmp_path, ".env.live")
    findings = scan_tracked_secrets(tmp_path)
    assert any(f.path == ".env.live" and f.kind == "FORBIDDEN_PRIVATE_FILE" for f in findings)


def test_reviewed_public_digest_context_never_hides_an_added_credential(tmp_path: Path) -> None:
    import base64
    import hashlib
    import json

    from aco.core.secret_scan import scan_tracked_secrets

    repository(tmp_path)
    integrity = base64.b64encode(hashlib.sha512(b"public-package-content").digest()).decode()
    lock = tmp_path / "pnpm-lock.yaml"
    lock.write_text(f"packages:\n  package@1:\n    resolution: {{integrity: sha512-{integrity}}}\n")
    evidence = tmp_path / "tests/fixtures/security/public-metadata.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(json.dumps({"source_sha256": hashlib.sha256(b"public-source").hexdigest()}, indent=2))
    stage(tmp_path, "pnpm-lock.yaml", "tests/fixtures/security/public-metadata.json")
    assert scan_tracked_secrets(tmp_path) == []
    marker = "-----BEGIN " + "RSA PRIVATE KEY-----"
    lock.write_text(lock.read_text() + f"credential: {marker!r}\n")
    evidence.write_text(json.dumps({"source_sha256": marker}, indent=2))
    findings = scan_tracked_secrets(tmp_path)
    assert {f.path for f in findings} == {"pnpm-lock.yaml", "tests/fixtures/security/public-metadata.json"}
    assert all(f.kind == "Private Key" for f in findings)


@pytest.mark.parametrize(
    ("relative", "needle"),
    [
        ("tests/unit/test_ingress_runtime_settings.py", "synthetic-private-secret"),
        ("playwright.config.ts", "SYNTHETIC_EMAIL_INGRESS_WEBHOOK_SECRET:"),
        ("tests/integration/test_agent_email_dispatch.py", "default_secret="),
        ("tests/integration/test_agent_email_live_code.py", "default_secret="),
        (
            "tests/fixtures/security/public-metadata.json",
            '"source_sha256":',
        ),
    ],
)
def test_reviewed_synthetic_line_is_exact_not_a_file_exclusion(
    tmp_path: Path, relative: str, needle: str
) -> None:
    from aco.core.secret_scan import scan_tracked_secrets

    repository(tmp_path)
    source = Path(__file__).resolve().parents[2] / relative
    line = next(line for line in source.read_text().splitlines() if needle in line)
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(line + "\n")
    stage(tmp_path, relative)
    assert scan_tracked_secrets(tmp_path) == []
    marker = "-----BEGIN " + "RSA PRIVATE KEY-----"
    target.write_text(line + f" # {marker}\n")
    assert any(f.kind == "Private Key" for f in scan_tracked_secrets(tmp_path))


@pytest.mark.parametrize(
    ("relative", "needle", "occurrence"),
    [
        ("tests/fixtures/security/public-metadata.json", '"source_sha256"', 0),
        ("tests/unit/test_provider_startup.py", '"OPENAI_API_KEY"', 0),
        ("tests/integration/test_combined_provider_startup.py", "offline-placeholder", 0),
        ("tests/voice_live_support.py", '"secretKey"', 0),
        ("tests/voice_application_support.py", '"capability_secret"', 0),
    ],
)
def test_reviewed_public_fixture_line_never_hides_an_added_credential(
    tmp_path: Path, relative: str, needle: str, occurrence: int
) -> None:
    from aco.core.secret_scan import scan_tracked_secrets

    repository(tmp_path)
    source = Path(__file__).resolve().parents[2] / relative
    line = [line for line in source.read_text().splitlines() if needle in line][occurrence]
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(line + "\n")
    stage(tmp_path, relative)
    assert scan_tracked_secrets(tmp_path) == []
    marker = "-----BEGIN " + "RSA PRIVATE KEY-----"
    target.write_text(line + f" # {marker}\n")
    assert any(f.kind == "Private Key" for f in scan_tracked_secrets(tmp_path))
