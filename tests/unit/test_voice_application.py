"""Explicit server selection must not inspect ambient private provider files."""

from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker

from aco.core.settings import Settings


def test_absent_voice_selection_reads_no_private_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    from aco.integrations.vapi import application

    def forbidden(*args: object) -> bytes:
        pytest.fail("Default startup cannot read private Voice configuration")

    monkeypatch.setattr(application, "read_private", forbidden)
    assert application.local_voice_runtime(Settings(), async_sessionmaker()) is None


@pytest.mark.parametrize("filename", [".env.live", "configuration.json"])
def test_selected_invalid_voice_configuration_is_sanitized(tmp_path: Path, filename: str) -> None:
    from aco.integrations.vapi import application

    path = tmp_path / filename
    path.write_text("not-json-private-fixture")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="^VOICE_PRIVATE_CONFIGURATION_REQUIRED$"):
        application.local_voice_runtime(
            Settings(database_url=SecretStr("postgresql+psycopg://unused:unused@127.0.0.1:1/unused")),
            async_sessionmaker(),
            config_path=path,
        )


@pytest.mark.parametrize("kind", ["public", "symlink", "ambient"])
def test_private_selection_refuses_public_symlink_or_ambient_file(tmp_path: Path, kind: str) -> None:
    from aco.integrations.vapi.application import read_private

    path = tmp_path / (".env.live" if kind == "ambient" else "configuration.json")
    path.write_bytes(b"explicit-synthetic-nonsecret-fixture")
    path.chmod(0o644 if kind == "public" else 0o600)
    if kind == "symlink":
        link = tmp_path / "selection.json"
        link.symlink_to(path)
        path = link
    with pytest.raises(ValueError, match="^VOICE_PRIVATE_CONFIGURATION_REQUIRED$"):
        read_private(path)
