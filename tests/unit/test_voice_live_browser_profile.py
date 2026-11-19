"""Test-only browser fixture selection cannot admit an ambient provider lane."""

from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from serve_voice_live_browser import fixture_transport
from voice_live_browser_state import BrowserFixture, require_profile

from aco.core.settings import Settings


def local() -> Settings:
    return Settings(database_url=SecretStr("postgresql+psycopg://aco:synthetic@127.0.0.1:55432/aco"))


def test_browser_fixture_rejects_default_public_profiles_and_real_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "ACO_AGENT_LIVE_CONFIG_PATH",
        "ACO_AGENT_LIVE_GRANT_ID",
        "ACO_EMAIL_LIVE_CONFIG_PATH",
        "ACO_VOICE_LIVE_CONFIG_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ACO_TEST_PROFILE", "mock")
    with pytest.raises(ValueError, match="Explicit contained"):
        require_profile(local())
    monkeypatch.setenv("ACO_TEST_PROFILE", "voice-live-code")
    require_profile(local())
    monkeypatch.setenv("ACO_EMAIL_LIVE_CONFIG_PATH", "/private/tmp/forbidden-real-selection")
    with pytest.raises(ValueError, match="Real provider selectors"):
        require_profile(local())


async def test_external_http_double_only_accepts_its_fixed_provider_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("serve_voice_live_browser.MANIFEST", tmp_path / "synthetic-fixture.json")
    fixture = BrowserFixture(
        tenant_id=uuid4(),
        connection_id=uuid4(),
        grant_id=uuid4(),
        assistant_id=uuid4(),
        credential_id=uuid4(),
        native_id=uuid4(),
        invoice_id=uuid4(),
        source_commit="a" * 40,
        participant_email="synthetic@example.test",
        join_encryption_key="not-used-in-transport-test",
        profile={"id": "actual-synthetic-profile-fixture"},
        credential={"id": "actual-synthetic-credential-fixture"},
    )
    async with httpx.AsyncClient(
        transport=fixture_transport(fixture), base_url="https://api.vapi.ai"
    ) as client:
        assert (await client.get(f"/assistant/{fixture.assistant_id}")).json() == fixture.profile
        assert (await client.get(f"/credential/{fixture.credential_id}")).json() == fixture.credential
        with pytest.raises(AssertionError, match="Unexpected external operation"):
            await client.get("/unapproved-operation")


@pytest.mark.parametrize("migration_fails", [False, True])
def test_profile_owned_schema_and_nonsecret_receipt_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, migration_fails: bool
) -> None:
    from identity_support import database_url
    from serve_voice_live_browser import owned_schema
    from sqlalchemy import create_engine, text
    from voice_live_browser_state import BrowserRuntimeReceipt, fixture_settings

    for name in (
        "ACO_AGENT_LIVE_CONFIG_PATH",
        "ACO_AGENT_LIVE_GRANT_ID",
        "ACO_EMAIL_LIVE_CONFIG_PATH",
        "ACO_VOICE_LIVE_CONFIG_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ACO_TEST_PROFILE", "voice-live-code")
    receipt_path = tmp_path / "synthetic-runtime.json"
    monkeypatch.setattr("serve_voice_live_browser.RUNTIME_RECEIPT", receipt_path)
    monkeypatch.setattr("voice_live_browser_state.RUNTIME_RECEIPT", receipt_path)
    monkeypatch.setattr("serve_voice_live_browser.current_source_commit", lambda: "a" * 40)
    monkeypatch.setattr("voice_live_browser_state.current_source_commit", lambda: "a" * 40)
    settings = Settings(database_url=SecretStr(database_url()))
    nonce = uuid4()
    schemas = [f"voice_live_browser_{nonce.hex}"]
    monkeypatch.setattr("serve_voice_live_browser.uuid4", lambda: nonce)
    if migration_fails:
        from alembic import command

        original_upgrade = command.upgrade

        def fail_migration(*args: object, **kwargs: object) -> None:
            raise ValueError("Explicit fixture migration failure")

        monkeypatch.setattr(command, "upgrade", fail_migration)
        with pytest.raises(ValueError, match="Explicit fixture migration failure"):
            with owned_schema(settings):
                pytest.fail("Failed fixture cannot become a usable API schema")
        monkeypatch.setattr(command, "upgrade", original_upgrade)
    else:
        with owned_schema(settings) as isolated:
            receipt = BrowserRuntimeReceipt.model_validate_json(receipt_path.read_text())
            assert receipt.schema_name == schemas[0]
            assert "database" not in receipt_path.read_text().lower()
            assert isolated.database_url is not None
            derived = fixture_settings(settings)
            assert derived.database_url == isolated.database_url
            engine = create_engine(isolated.database_url.get_secret_value())
            try:
                with engine.connect() as connection:
                    assert connection.scalar(text("select current_schema()")) == receipt.schema_name
                    assert connection.scalar(text("select count(*) from voice_live_grants")) == 0
            finally:
                engine.dispose()
    assert not receipt_path.exists()
    engine = create_engine(database_url())
    try:
        with engine.connect() as connection:
            for schema in schemas:
                assert (
                    connection.scalar(
                        text("select count(*) from pg_namespace where nspname=:name"), {"name": schema}
                    )
                    == 0
                )
    finally:
        engine.dispose()
