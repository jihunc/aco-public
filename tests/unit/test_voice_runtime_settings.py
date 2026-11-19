"""Explicit offline voice composition cannot enable live, capture or external callbacks."""

from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, Request
from pydantic import SecretStr, ValidationError

from aco.api.app import create_app
from aco.core.local_runtime import local_settings
from aco.core.settings import Settings, load_settings


def test_voice_selection_is_explicit_local_and_keeps_other_authorities_disabled() -> None:
    assert not Settings().synthetic_voice_enabled
    selected = local_settings({"SYNTHETIC_VOICE_ENABLED": "true"})
    assert selected.synthetic_voice_enabled
    assert selected.kill_switch and not selected.outbound_enabled
    assert not selected.recording_enabled and not selected.transcription_enabled
    demo = load_settings(
        {"APP_ENV": "demo", "WEB_PUBLIC_URL": "https://aco.example.test", "SYNTHETIC_VOICE_ENABLED": "true"}
    )
    assert demo.synthetic_voice_enabled
    with pytest.raises(ValidationError, match="Synthetic voice"):
        load_settings(
            {
                "APP_ENV": "production",
                "WEB_PUBLIC_URL": "https://aco.example.test",
                "SYNTHETIC_VOICE_ENABLED": "true",
            }
        )
    with pytest.raises(ValidationError):
        local_settings({"APP_ENV": "production", "SYNTHETIC_VOICE_ENABLED": "true"})


@pytest.mark.parametrize("enabled", [False, True])
async def test_app_composes_only_selected_offline_factory_and_preserves_disabled_history(
    enabled: bool,
) -> None:
    from aco.features.voice.runtime import VoiceRuntime
    from aco.features.voice.setup import connection_key

    settings = Settings(
        database_url=SecretStr("postgresql+psycopg://unused@127.0.0.1:55432/unused"),
        identity_issuer="https://identity.synthetic.invalid/aco",
        identity_discovery_url="https://identity.synthetic.invalid/aco/.well-known/openid-configuration",
        identity_client_secret=SecretStr("synthetic"),
        session_secret=SecretStr("synthetic-session-secret-32-bytes-minimum"),
        token_encryption_key=SecretStr(Fernet.generate_key().decode()),
        synthetic_voice_enabled=enabled,
    )
    app = create_app(settings)
    runtime = app.state.voice_runtime
    assert isinstance(runtime, VoiceRuntime)
    assert runtime.enabled is enabled
    assert runtime.setup_enabled is enabled
    assert "/api/workspaces/{workspace_id}/voice-sessions/{session_id}" in app.openapi()["paths"]
    tenant = uuid4()
    if enabled:
        assert runtime.binding(tenant, connection_key(tenant)).evidence_mode == "MOCKED_HTTP"
    else:
        with pytest.raises(HTTPException):
            runtime.binding(tenant, connection_key(tenant))
    app.state.sessions.kw["bind"].dispose()
    await app.state.business_sessions.kw["bind"].dispose()


@pytest.mark.parametrize("status", [200, 403])
async def test_server_callback_sender_uses_exact_route_bytes_headers_and_sanitizes_failure(
    status: int,
) -> None:
    from aco.api.app import _send_offline_voice_callback

    app = FastAPI()
    seen: list[bytes] = []

    @app.post("/api/voice/callback")
    async def callback(request: Request) -> dict[str, str]:
        seen.append(await request.body())
        assert request.headers["x-signature"] == "synthetic-signature"
        assert request.headers["x-timestamp"] == "123"
        assert "authorization" not in request.headers
        if status != 200:
            raise HTTPException(status, "private-provider-error")
        return {"status": "received"}

    raw = b'{ "signed": "exact\\nbytes" }'
    headers = {
        "content-type": "application/json",
        "x-signature": "synthetic-signature",
        "x-timestamp": "123",
        "authorization": "must-not-forward",
    }
    if status == 200:
        await _send_offline_voice_callback(app, raw, headers)
    else:
        with pytest.raises(HTTPException) as error:
            await _send_offline_voice_callback(app, raw, headers)
        assert error.value.status_code == 503
        assert error.value.detail == "VOICE_OFFLINE_CALLBACK_UNAVAILABLE"
    assert seen == [raw]
