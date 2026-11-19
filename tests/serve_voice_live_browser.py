"""Contained test-only API: real application, only Vapi HTTP responses are doubled.

No selectable production factory, credential, provider account or real grant is read.
"""

import asyncio
import json
import multiprocessing
import os
import signal
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

import httpx
import uvicorn
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from starlette.middleware.base import RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from voice_live_browser_state import (
    CAPABILITY,
    MANIFEST,
    RUNTIME_RECEIPT,
    BrowserFixture,
    BrowserRuntimeReceipt,
    read_fixture,
    require_profile,
)

from aco.api.app import create_app
from aco.core.live import DELIVERY_LIVE_AUTHORIZATION
from aco.core.local_runtime import local_settings
from aco.core.settings import Settings
from aco.features.voice.live import VoiceLiveAuthorization
from aco.features.voice.live_runtime import LiveVoiceRuntime
from aco.integrations.provider_smoke.source import current_source_commit
from alembic import command


def fixture_transport(fixture: BrowserFixture) -> httpx.MockTransport:
    observed = MANIFEST.with_name("voice-live-browser-http-observed.json")
    calls: list[dict[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append({"method": request.method, "path": request.url.path})
        observed.write_text(json.dumps(calls))
        if request.method == "GET" and request.url.path == f"/assistant/{fixture.assistant_id}":
            return httpx.Response(200, json=fixture.profile)
        if request.method == "GET" and request.url.path == f"/credential/{fixture.credential_id}":
            return httpx.Response(200, json=fixture.credential)
        if request.method == "POST" and request.url.path == "/call/web":
            assert request.headers["authorization"] == "Bearer synthetic-no-provider-key"
            return httpx.Response(
                201,
                json={
                    "id": str(fixture.native_id),
                    "assistantId": str(fixture.assistant_id),
                    "type": "webCall",
                    "status": "queued",
                    "webCallUrl": "https://vapi.daily.co/synthetic-owned-browser-room",
                    "monitor": {"controlUrl": f"https://control.vapi.ai/{fixture.native_id}/control"},
                },
            )
        if request.method == "POST" and request.url == f"https://control.vapi.ai/{fixture.native_id}/control":
            assert request.headers["authorization"] == "Bearer synthetic-no-control-key"
            assert json.loads(request.content) == {"type": "end-call"}
            return httpx.Response(200, json={"status": "ok"})
        raise AssertionError("Unexpected external operation in fixed synthetic Voice browser fixture")

    return httpx.MockTransport(respond)


@contextmanager
def owned_schema(settings: Settings) -> Iterator[Settings]:
    require_profile(settings)
    if settings.database_url is None or RUNTIME_RECEIPT.exists():
        raise ValueError("Fresh owned browser runtime receipt required")
    base = create_engine(settings.database_url.get_secret_value())
    receipt = BrowserRuntimeReceipt(
        schema_name="voice_live_browser_" + uuid4().hex,
        source_commit=current_source_commit(),
        pid=os.getpid(),
    )
    url = make_url(settings.database_url.get_secret_value()).update_query_dict(
        {"options": f"-csearch_path={receipt.schema_name}"}
    )
    created = False
    try:
        with base.begin() as connection:
            connection.execute(text(f"CREATE SCHEMA {receipt.schema_name}"))
        created = True
        configuration = Config("alembic.ini")
        configuration.set_main_option(
            "sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%")
        )
        command.upgrade(configuration, "head")
        RUNTIME_RECEIPT.parent.mkdir(exist_ok=True)
        descriptor = os.open(RUNTIME_RECEIPT, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(receipt.model_dump_json())
        yield Settings.model_validate(
            settings.model_dump() | {"database_url": SecretStr(url.render_as_string(hide_password=False))}
        )
    finally:
        if created:
            with base.begin() as connection:
                connection.execute(text(f"DROP SCHEMA {receipt.schema_name} CASCADE"))
        if RUNTIME_RECEIPT.exists() and RUNTIME_RECEIPT.read_text() == receipt.model_dump_json():
            RUNTIME_RECEIPT.unlink()
        for path in (MANIFEST, MANIFEST.with_name("voice-live-browser-http-observed.json")):
            path.unlink(missing_ok=True)
        base.dispose()


def serve(settings: Settings) -> None:
    # This test-only explicit selection is validated as local/demo metadata. It is
    # not a private operator config and is never forwarded to production loaders.
    selected = Settings.model_validate(
        settings.model_dump()
        | {
            "voice_live_config_path": MANIFEST,
            "outbound_enabled": True,
            "kill_switch": False,
            "synthetic_voice_enabled": False,
            "synthetic_email_enabled": False,
        }
    )
    app = create_app(selected)
    cache: dict[str, LiveVoiceRuntime] = {}

    @app.middleware("http")
    async def select_fixture(request: Request, call_next: RequestResponseEndpoint) -> Response:
        if (
            "/voice-sessions" in request.url.path or request.url.path == "/api/voice/callback"
        ) and MANIFEST.exists():
            fixture = read_fixture()
            if fixture.source_commit != current_source_commit():
                raise RuntimeError("Synthetic browser fixture source changed")
            key = str(fixture.grant_id)
            if key not in cache:
                binding = fixture.binding()
                transport = fixture_transport(fixture)
                cache[key] = LiveVoiceRuntime(
                    app.state.business_sessions,
                    binding,
                    authorization=VoiceLiveAuthorization(
                        fixture.grant_id,
                        authorization_ref=DELIVERY_LIVE_AUTHORIZATION,
                        source_commit=fixture.source_commit,
                        kill_switch=lambda: selected.kill_switch or not selected.outbound_enabled,
                        integrity_check=lambda: fixture.source_commit == current_source_commit(),
                    ),
                    capability_secret=SecretStr(CAPABILITY),
                    join_encryption_key=SecretStr(fixture.join_encryption_key),
                    transport_factory=lambda: transport,
                )
            app.state.voice_runtime = cache[key]
        return await call_next(request)

    try:
        uvicorn.run(app, host="127.0.0.1", port=58000, access_log=False)
    finally:
        if hasattr(app.state, "business_sessions"):
            asyncio.run(app.state.business_sessions.kw["bind"].dispose())
            app.state.sessions.kw["bind"].dispose()
        if MANIFEST.exists():
            fixture = read_fixture()
            if str(fixture.grant_id) in cache:
                MANIFEST.unlink()
                MANIFEST.with_name("voice-live-browser-http-observed.json").unlink(missing_ok=True)


def main() -> None:
    # Uvicorn re-raises SIGTERM after graceful shutdown. The separate parent
    # retains fixture cleanup authority without changing the actual server.
    with owned_schema(local_settings()) as settings:
        child = multiprocessing.get_context("spawn").Process(target=serve, args=(settings,))
        child.start()
        stopped = False

        def stop_owned_child(signum: int, frame: object) -> None:
            nonlocal stopped
            stopped = True
            if child.is_alive():
                child.terminate()

        prior = {
            signum: signal.signal(signum, stop_owned_child) for signum in (signal.SIGTERM, signal.SIGINT)
        }
        try:
            child.join()
            if child.exitcode and not stopped:
                raise RuntimeError("Owned synthetic Voice API child failed")
        finally:
            if child.is_alive():
                child.terminate()
                child.join(timeout=5)
                if child.is_alive():
                    child.kill()
                    child.join()
            for signum, handler in prior.items():
                signal.signal(signum, handler)
    print("OWNED_VOICE_SCHEMA_AND_FIXTURES_REMOVED", flush=True)


if __name__ == "__main__":
    main()
