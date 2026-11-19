"""Private selection is opt-in; this test never authorizes or contacts a provider."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aco.core.settings import Settings
from aco.integrations.resend.application import load_private_config, local_email_runtime


def config_material(tmp_path: Path) -> dict[str, object]:
    return {
        "grant_id": str(uuid4()),
        "tenant_id": str(uuid4()),
        "connection_id": str(uuid4()),
        "authorization_ref": "offline-selection",
        "source_commit": "a" * 40,
        "credential_file": str(tmp_path / "private-credentials.json"),
        "credential_sha256": "b" * 64,
        "download_hosts": ["files.example.test"],
        "webhook_endpoint_id": str(uuid4()),
        "kill_switch": False,
    }


def test_no_selection_never_reads_config_database_or_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> bytes:
        pytest.fail("Default application must not read a private selection or credentials")

    monkeypatch.setattr("aco.integrations.resend.application.read_private", forbidden)
    assert local_email_runtime(Settings(), async_sessionmaker[AsyncSession](), None) is None


def test_private_selection_rejects_mode_symlink_and_unknown_material(tmp_path: Path) -> None:
    path = tmp_path / "selection.json"
    path.write_text(json.dumps(config_material(tmp_path)))
    path.chmod(0o644)
    with pytest.raises(ValueError, match="EMAIL_PRIVATE_CONFIGURATION_REQUIRED"):
        load_private_config(path)
    path.chmod(0o600)
    assert load_private_config(path).kill_switch is False
    alias = tmp_path / "alias.json"
    alias.symlink_to(path)
    with pytest.raises(ValueError, match="EMAIL_PRIVATE_CONFIGURATION_REQUIRED"):
        load_private_config(alias)
    path.write_text(json.dumps({**config_material(tmp_path), "api_key": "must-not-be-config-material"}))
    with pytest.raises(ValueError, match="EMAIL_PRIVATE_CONFIGURATION_REQUIRED"):
        load_private_config(path)
