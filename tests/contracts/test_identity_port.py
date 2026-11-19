"""The public login boundary must exist and fail closed without configuration."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from aco.api.app import create_app
from aco.core.settings import Settings


def test_unconfigured_login_is_typed_unavailable() -> None:
    response = TestClient(create_app(Settings())).get("/api/auth/login", follow_redirects=False)
    assert response.status_code == 503
    assert response.json()["detail"] == "TEMPORARILY_UNAVAILABLE"


def test_signed_id_claims_state_nonce_audience_issuer_and_replay() -> None:
    """Exercise ACO's callback consumer, not the JWT library in isolation."""
    from typing import Any, cast
    from urllib.parse import parse_qs, urlsplit

    import httpx2
    import pytest
    import respx
    from fastapi import FastAPI
    from identity_support import actor_client
    from joserfc import jwt
    from joserfc.jwk import RSAKey
    from starlette.requests import Request

    from aco.features.identity.models import LoginAttempt, now
    from aco.integrations.ports.identity import IdentityFailure

    with actor_client("owner") as (client, store, _, _):
        port = cast(FastAPI, client.app).state.identity
        key = RSAKey.generate_key(2048)
        key.ensure_kid()
        with respx.mock(assert_all_called=False) as router:
            router.get(port.settings.identity_issuer + "/jwks_uri").respond(
                200, json={"keys": [key.as_dict(private=False)]}
            )
            for wrong in ("state", "nonce", "aud", "iss", "signature", "expired", "none"):
                response = asyncio.run(
                    port.begin_login(Request({"type": "http", "method": "GET", "headers": []}))
                )
                state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
                assert parse_qs(urlsplit(response.headers["location"]).query)["code_challenge_method"] == [
                    "S256"
                ]
                attempt = store.query(LoginAttempt).filter_by(state=state).one()
                claims = {
                    "iss": port.settings.identity_issuer,
                    "sub": "synthetic-signed-subject",
                    "aud": "aco",
                    "iat": int(now().timestamp()),
                    "exp": int(now().timestamp()) + 300,
                    "nonce": port.reveal(attempt.encrypted_payload)["nonce"],
                    "email": "signed@example.test",
                    "email_verified": True,
                }
                if wrong in ("nonce", "aud", "iss"):
                    claims[wrong] = "wrong"
                if wrong == "expired":
                    claims["exp"] = int(now().timestamp()) - 1
                signing_key = RSAKey.generate_key(2048) if wrong == "signature" else key
                token = jwt.encode({"alg": "RS256", "kid": key.kid}, claims, signing_key)

                def exchange(request: Any, signed_token: str = token) -> Any:
                    return httpx2.Response(
                        200,
                        json={
                            "access_token": "synthetic-access",
                            "refresh_token": "synthetic-refresh",
                            "id_token": signed_token,
                            "expires_in": 300,
                            "token_type": "Bearer",
                        },
                    )

                port.oauth_transport = httpx2.MockTransport(exchange)
                cookie = port.signer.dumps({"state": "wrong" if wrong == "state" else state})
                request = Request(
                    {
                        "type": "http",
                        "method": "GET",
                        "scheme": "http",
                        "server": ("127.0.0.1", 53000),
                        "path": "/api/auth/callback",
                        "query_string": f"code=synthetic-code&state={state}".encode(),
                        "headers": [(b"cookie", f"aco_login={cookie}".encode())],
                    }
                )
                if wrong != "none":
                    with pytest.raises(IdentityFailure) as failure:
                        asyncio.run(port.complete_login(request))
                    assert failure.value.failure.code == "AUTH_EXPIRED"
                else:
                    completed = asyncio.run(port.complete_login(request))
                    assert completed.status_code == 302 and completed.headers["location"].endswith(
                        "/onboarding"
                    )
                    assert "HttpOnly" in completed.headers["set-cookie"]
                    with pytest.raises(IdentityFailure):
                        asyncio.run(port.complete_login(request))


def test_identity_failure_capabilities_and_no_secret_error_output() -> None:
    from typing import cast

    import httpx
    import pytest
    from fastapi import FastAPI
    from identity_support import actor_client

    from aco.integrations.ports.identity import IdentityFailure

    with actor_client("owner") as (client, _, _, _):
        port = cast(FastAPI, client.app).state.identity
        assert asyncio.run(port.validate_connection()) is True
        assert {"pkce_s256", "server_session", "revocation"} <= asyncio.run(
            port.describe_capabilities()
        ).features
        for status, expected in [
            (401, "AUTH_EXPIRED"),
            (403, "AUTH_EXPIRED"),
            (429, "RATE_LIMITED"),
            (503, "TEMPORARILY_UNAVAILABLE"),
            (400, "VALIDATION_FAILED"),
        ]:
            with pytest.raises(IdentityFailure) as error:
                port.check_response(httpx.Response(status, text="NEVER_DISCLOSE_PROVIDER_SECRET"))
            assert error.value.failure.code == expected
            assert "NEVER_DISCLOSE" not in str(error.value)
            assert error.value.failure.may_have_side_effect is False


def test_identity_operations_retain_provenance_and_unknown_cost() -> None:
    from identity_support import actor_client
    from sqlalchemy import text

    with actor_client("owner") as (client, store, user, workspace):
        assert client.get(f"/api/workspaces/{workspace}").status_code == 200
        # Query the real persisted provider result ledger. Missing table is not the initial RED gate.
        names = set(
            store.execute(text("SELECT tablename FROM pg_tables WHERE schemaname=current_schema()")).scalars()
        )
        assert "identity_provider_events" in names
        rows = store.execute(
            text(
                "SELECT operation, provider, native_id, cost_quality, cost_amount, outcome "
                "FROM identity_provider_events"
            )
        ).all()
        assert any(
            row.operation == "authenticate_request"
            and row.native_id == user.subject
            and row.provider == user.issuer
            and row.cost_quality == "UNKNOWN"
            and row.cost_amount is None
            and row.outcome == "VERIFIED"
            for row in rows
        )


def test_identity_provider_outage_and_revocation_are_distinct() -> None:
    from typing import Any, cast

    import httpx2
    from fastapi import FastAPI
    from identity_support import actor_client

    with actor_client("owner") as (client, _, _, _):
        port = cast(FastAPI, client.app).state.identity

        def unavailable(request: Any) -> Any:
            raise httpx2.ConnectError("synthetic private provider detail")

        port.oauth_transport = httpx2.MockTransport(unavailable)
        response = client.get("/api/me")
        assert response.status_code == 503
        assert response.json()["detail"] == "TEMPORARILY_UNAVAILABLE"
        port.oauth_transport = httpx2.MockTransport(
            lambda request: httpx2.Response(200, json={"active": False})
        )
        assert client.get("/api/me").status_code == 401


def test_provider_failure_preserves_retry_and_request_identity() -> None:
    from datetime import timedelta
    from typing import cast

    import httpx
    import pytest
    from fastapi import FastAPI
    from identity_support import actor_client

    from aco.integrations.ports.identity import IdentityFailure

    with actor_client("owner") as (client, _, _, _):
        port = cast(FastAPI, client.app).state.identity
        with pytest.raises(IdentityFailure) as failure:
            port.check_response(
                httpx.Response(429, headers={"retry-after": "17", "x-request-id": "synthetic-request-1"})
            )
        assert failure.value.failure.retry_after == timedelta(seconds=17)
        assert failure.value.failure.request_id == "synthetic-request-1"


def test_missing_pkce_and_uncertain_rotation_fail_closed() -> None:
    from typing import Any, cast

    import httpx2
    import respx
    from fastapi import FastAPI
    from identity_support import actor_client

    with actor_client("owner") as (client, store, user, _):
        port = cast(FastAPI, client.app).state.identity
        original_settings = port.settings
        port.settings = port.settings.model_copy(
            update={"identity_discovery_url": port.settings.identity_issuer + "/missing-pkce"}
        )
        with respx.mock(assert_all_called=False) as router:
            router.get(port.settings.identity_discovery_url).respond(
                200, json={"issuer": port.settings.identity_issuer, "code_challenge_methods_supported": []}
            )
            response = client.get("/api/auth/login", follow_redirects=False)
            assert response.status_code == 422
            assert response.json()["detail"] == "UNSUPPORTED_CAPABILITY"
        port.settings = original_settings
        me = client.get("/api/me").json()
        before = client.cookies.get("aco_session")

        def lost_rotation(request: Any) -> Any:
            if request.url.path.endswith("introspection_endpoint"):
                return httpx2.Response(200, json={"active": True, "sub": user.subject})
            raise httpx2.ReadTimeout("token response lost after remote mutation")

        # The token server may already have rotated: don't imply safe retry or mint a session.
        port.oauth_transport = httpx2.MockTransport(lost_rotation)
        response = client.post(
            "/api/auth/refresh",
            headers={"origin": port.settings.web_public_url, "x-csrf-token": me["csrf_token"]},
        )
        assert response.status_code == 503
        assert response.json()["detail"] == "UNKNOWN_SIDE_EFFECT"
        assert client.cookies.get("aco_session") == before
        assert client.get("/api/me").status_code == 401  # Uncertain refresh is never automatically replayed.


def test_identity_port_public_operations_are_async() -> None:
    import inspect
    from typing import cast

    from fastapi import FastAPI
    from identity_support import actor_client

    with actor_client("owner") as (client, _, _, _):
        port = cast(FastAPI, client.app).state.identity
        for method in (
            "begin_login",
            "complete_login",
            "authenticate_request",
            "rotate",
            "logout",
            "validate_connection",
            "describe_capabilities",
        ):
            assert inspect.iscoroutinefunction(getattr(port, method)), method


def test_runtime_registry_exposes_the_actual_identity_port() -> None:
    from typing import cast

    from fastapi import FastAPI
    from identity_support import actor_client

    with actor_client("owner") as (client, _, _, _):
        app = cast(FastAPI, client.app)
        assert app.state.ports.identity is app.state.identity
        assert app.state.ports.provider_modes()["identity"] == "configured"
        from aco.integrations.demo_email.adapter import DemoEmailRuntime

        assert isinstance(app.state.ports.email, DemoEmailRuntime)
        assert app.state.ports.email.sessions is app.state.business_sessions
        assert app.state.ports.provider_modes()["email"] == "configured"
        assert app.state.settings.external_providers == "fake"
        assert app.state.settings.outbound_enabled is False
        assert app.state.settings.kill_switch is True


def test_authentication_provenance_uses_the_existing_transaction() -> None:
    from typing import cast

    from fastapi import FastAPI
    from identity_support import actor_client
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    with actor_client("owner") as (client, _, _, _):
        port = cast(FastAPI, client.app).state.identity
        engine = create_engine(port.sessions.kw["bind"].url, pool_size=1, max_overflow=0, pool_timeout=0.1)
        port.sessions = sessionmaker(engine, expire_on_commit=False)
        try:
            assert client.get("/api/me").status_code == 200
        finally:
            engine.dispose()


def test_callback_jwks_outage_retains_one_failed_operation_and_consumed_code() -> None:
    from typing import Any
    from unittest.mock import patch
    from urllib.parse import parse_qs, urlsplit

    import httpx
    import httpx2
    from contracts.test_identity_swappability import identity_shape

    from aco.features.identity.models import IdentityProviderEvent, LoginSession

    with identity_shape("oidc") as (client, store, _, _, port, grant):
        started = client.get("/api/auth/login", follow_redirects=False)
        state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
        callback = "/api/auth/callback?state=" + state + "&code=" + grant(state)
        original_get = httpx.Client.get
        original_transport = port.oauth_transport  # type: ignore[attr-defined]
        exchanges: list[bool] = []

        def transport(request: Any) -> Any:
            if b"grant_type=authorization_code" in request.content:
                exchanges.append(True)
            return original_transport.handle_request(request)

        port.oauth_transport = httpx2.MockTransport(transport)  # type: ignore[attr-defined]

        def outage_get(self: httpx.Client, url: Any, **kwargs: Any) -> httpx.Response:
            if str(url).endswith("/jwks_uri"):
                raise httpx.ConnectError("synthetic key server unavailable")
            return original_get(self, url, **kwargs)

        sessions_before = store.query(LoginSession).count()
        with patch.object(httpx.Client, "get", outage_get):
            response = client.get(callback, follow_redirects=False)
        assert response.status_code == 503
        assert response.json() == {"detail": "TEMPORARILY_UNAVAILABLE"}
        store.expire_all()
        failed = store.query(IdentityProviderEvent).filter_by(operation="complete_login").all()
        assert len(failed) == 1
        assert failed[0].outcome == "TEMPORARILY_UNAVAILABLE"
        assert failed[0].correlation_id and failed[0].connection_id
        assert failed[0].cost_quality == "UNKNOWN" and failed[0].cost_amount is None
        assert len(exchanges) == 1 and store.query(LoginSession).count() == sessions_before
        assert client.get(callback, follow_redirects=False).status_code == 401
        assert len(exchanges) == 1


@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("cleanup_outage", [False, True])
def test_refresh_commit_failure_fails_closed_across_adapter_restart(
    automatic: bool, cleanup_outage: bool
) -> None:
    """Fault occurs AFTER one confirmed remote refresh, including failed cleanup."""
    from dataclasses import replace
    from typing import Any, cast
    from uuid import UUID

    import httpx2
    from contracts.test_identity_swappability import identity_shape, login
    from fastapi import FastAPI
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    from aco.features.identity.models import IdentityProviderEvent, LoginSession
    from aco.integrations.oidc.adapter import OIDCAdapter

    with identity_shape("oidc") as (client, store, _, _, port, grant):
        login(client, grant)
        me = client.get("/api/me").json()
        client.headers["x-csrf-token"] = me["csrf_token"]
        adapter = cast(OIDCAdapter, port)
        cookie = client.cookies.get("aco_session")
        assert cookie is not None
        session_id = UUID(adapter.signer.loads(cookie))
        old = store.get(LoginSession, session_id)
        assert old is not None
        if automatic:
            token = adapter.reveal(old.encrypted_token)
            token["expires_at"] = 0
            old.encrypted_token = adapter.protect(token)
            store.commit()
        original_transport = adapter.oauth_transport
        refresh_calls: list[bool] = []
        final_commit_faults: list[bool] = []
        cleanup_faults: list[bool] = []
        pending_ids: list[tuple[UUID, UUID]] = []
        operation = "authenticate_request" if automatic else "rotate"

        def counted_transport(request: Any) -> Any:
            response = original_transport.handle_request(request)
            if b"grant_type=refresh_token" in request.content:
                refresh_calls.append(True)
                body = response.json()
                body["refresh_token"] = "rotated-synthetic-refresh"
                return httpx2.Response(response.status_code, json=body)
            return response

        adapter.oauth_transport = httpx2.MockTransport(counted_transport)

        def fail_commit(db: Session) -> None:
            # Include dirty rows: durable intent may already exist before remote work.
            events = [row for row in (*db.new, *db.dirty) if isinstance(row, IdentityProviderEvent)]
            pending_ids.extend(
                (row.id, row.correlation_id)
                for row in events
                if row.operation == operation and row.outcome == "PENDING"
            )
            if (
                len(refresh_calls) == 1
                and not final_commit_faults
                and any(row.operation == operation and row.outcome == "VERIFIED" for row in events)
            ):
                final_commit_faults.append(True)
                raise RuntimeError("synthetic final commit failure AFTER remote refresh")
            if cleanup_outage and final_commit_faults:
                cleanup_faults.append(True)
                raise RuntimeError("synthetic recovery database outage")

        caller = TestClient(client.app, base_url="http://127.0.0.1:53000", raise_server_exceptions=False)
        caller.cookies.update(client.cookies)
        caller.headers.update(client.headers)
        event.listen(Session, "before_commit", fail_commit)
        try:
            response = caller.get("/api/me") if automatic else caller.post("/api/auth/refresh")
        finally:
            event.remove(Session, "before_commit", fail_commit)
        assert len(refresh_calls) == 1 and len(final_commit_faults) == 1
        assert response.status_code == 503
        assert response.json() == {"detail": "UNKNOWN_SIDE_EFFECT"}
        if cleanup_outage:
            assert cleanup_faults
        # A fresh adapter has no process-local revoked-cookie cache to save it.
        app = cast(FastAPI, client.app)
        restarted = OIDCAdapter(adapter.settings, adapter.sessions, adapter.oauth_transport)
        app.state.identity = restarted
        app.state.ports = replace(app.state.ports, identity=restarted)
        assert client.get("/api/me").status_code == 401
        assert client.post("/api/auth/refresh").status_code == 401
        assert len(refresh_calls) == 1
        store.expire_all()
        old = store.get(LoginSession, session_id)
        assert old is not None and old.revoked is True
        failures = (
            store.query(IdentityProviderEvent)
            .filter_by(operation=operation, outcome="UNKNOWN_SIDE_EFFECT")
            .all()
        )
        assert len(failures) == 1
        assert failures[0].connection_id and failures[0].correlation_id
        assert pending_ids == [(failures[0].id, failures[0].correlation_id)]
        signed_out = client.post("/api/auth/logout")
        assert signed_out.status_code == 200 and signed_out.json()["provider_revoked"] is False
        assert len(refresh_calls) == 1
        assert failures[0].cost_quality == "UNKNOWN" and failures[0].cost_amount is None


@pytest.mark.parametrize("operation", ["complete_login", "authenticate_request"])
def test_local_commit_failure_is_not_invalid_identity(operation: str) -> None:
    from typing import Any
    from urllib.parse import parse_qs, urlsplit
    from uuid import UUID

    import httpx2
    from contracts.test_identity_swappability import identity_shape, login
    from sqlalchemy import event
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.orm import Session

    from aco.features.identity.models import IdentityProviderEvent, LoginSession

    with identity_shape("oidc") as (client, store, user, _, port, grant):
        if operation == "authenticate_request":
            login(client, grant)
            target = "/api/me"
        else:
            started = client.get("/api/auth/login", follow_redirects=False)
            state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
            target = "/api/auth/callback?state=" + state + "&code=" + grant(state)
        sessions_before = store.query(LoginSession).count()
        original_transport = port.oauth_transport  # type: ignore[attr-defined]
        exchanges: list[bool] = []
        faults: list[bool] = []
        introspections: list[bool] = []
        verified_events: list[tuple[UUID, UUID, int]] = []

        def counted(request: Any) -> Any:
            result = original_transport.handle_request(request)
            if b"grant_type=authorization_code" in request.content:
                exchanges.append(True)
            if str(request.url).endswith("/introspection_endpoint"):
                assert result.json() == {"active": True, "sub": user.subject}
                introspections.append(True)
            return result

        port.oauth_transport = httpx2.MockTransport(counted)  # type: ignore[attr-defined]

        def fail_commit(db: Session) -> None:
            for row in (*db.new, *db.dirty):
                if (
                    isinstance(row, IdentityProviderEvent)
                    and row.operation == operation
                    and row.outcome == "VERIFIED"
                ):
                    assert row.native_id == user.subject
                    db.flush()
                    verified_events.append((row.id, row.correlation_id, row.attempt))
                    faults.append(True)
                    raise SQLAlchemyError("synthetic commit failure after verified native subject")

        event.listen(Session, "before_commit", fail_commit)
        try:
            response = client.get(target, follow_redirects=False)
        finally:
            event.remove(Session, "before_commit", fail_commit)
        assert faults == [True]
        expected = "UNKNOWN_SIDE_EFFECT" if operation == "complete_login" else "TEMPORARILY_UNAVAILABLE"
        assert response.status_code == 503 and response.json() == {"detail": expected}
        store.expire_all()
        failures = store.query(IdentityProviderEvent).filter_by(operation=operation, outcome=expected).all()
        assert len(failures) == 1 and failures[0].correlation_id
        assert failures[0].native_id == user.subject
        assert verified_events == [(failures[0].id, failures[0].correlation_id, failures[0].attempt)]
        assert store.query(IdentityProviderEvent).filter_by(operation=operation).count() == 1
        assert failures[0].cost_quality == "UNKNOWN" and failures[0].cost_amount is None
        assert store.query(LoginSession).count() == sessions_before
        if operation == "complete_login":
            assert exchanges == [True]
            assert client.get(target, follow_redirects=False).status_code == 401
            assert exchanges == [True]
        else:
            assert introspections == [True]
            assert client.get(target).status_code == 200


@pytest.mark.parametrize("automatic", [False, True])
def test_post_refresh_jwks_outage_is_unavailable_and_never_replayed(automatic: bool) -> None:
    from typing import Any, cast
    from unittest.mock import patch

    import httpx
    import httpx2
    from contracts.test_identity_swappability import identity_shape, login

    from aco.features.identity.models import IdentityProviderEvent, LoginSession
    from aco.integrations.oidc.adapter import OIDCAdapter

    with identity_shape("oidc") as (client, store, _, _, port, grant):
        login(client, grant)
        client.headers["x-csrf-token"] = client.get("/api/me").json()["csrf_token"]
        adapter = cast(OIDCAdapter, port)
        cookie = client.cookies.get("aco_session")
        assert cookie is not None
        old = store.get(LoginSession, adapter.signer.loads(cookie))
        assert old is not None
        if automatic:
            token = adapter.reveal(old.encrypted_token)
            token["expires_at"] = 0
            old.encrypted_token = adapter.protect(token)
            store.commit()
        original_transport, original_get = adapter.oauth_transport, httpx.Client.get
        refreshes: list[bool] = []

        def transport(request: Any) -> Any:
            response = original_transport.handle_request(request)
            if b"grant_type=refresh_token" in request.content:
                refreshes.append(True)
            return response

        def outage_get(self: httpx.Client, url: Any, **kwargs: Any) -> httpx.Response:
            if str(url).endswith("/jwks_uri"):
                raise httpx.ConnectError("synthetic key service outage")
            return original_get(self, url, **kwargs)

        adapter.oauth_transport = httpx2.MockTransport(transport)
        with patch.object(httpx.Client, "get", outage_get):
            response = client.get("/api/me") if automatic else client.post("/api/auth/refresh")
        assert response.status_code == 503 and response.json() == {"detail": "TEMPORARILY_UNAVAILABLE"}
        assert client.get("/api/me").status_code == 401
        assert client.post("/api/auth/refresh").status_code == 401
        assert refreshes == [True]
        operation = "authenticate_request" if automatic else "rotate"
        failures = (
            store.query(IdentityProviderEvent)
            .filter_by(operation=operation, outcome="TEMPORARILY_UNAVAILABLE")
            .all()
        )
        assert len(failures) == 1 and failures[0].correlation_id
        assert failures[0].cost_quality == "UNKNOWN" and failures[0].cost_amount is None


@pytest.mark.parametrize("operation", ["complete_login", "rotate"])
def test_malformed_success_token_response_retains_unknown_effect(operation: str) -> None:
    from typing import Any, cast
    from urllib.parse import parse_qs, urlsplit

    import httpx2
    from contracts.test_identity_swappability import identity_shape, login

    from aco.features.identity.models import IdentityProviderEvent
    from aco.integrations.oidc.adapter import OIDCAdapter

    with identity_shape("oidc") as (client, store, _, _, port, grant):
        if operation == "rotate":
            login(client, grant)
            client.headers["x-csrf-token"] = client.get("/api/me").json()["csrf_token"]
            target = "/api/auth/refresh"
        else:
            started = client.get("/api/auth/login", follow_redirects=False)
            state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
            target = "/api/auth/callback?state=" + state + "&code=" + grant(state)
        adapter = cast(OIDCAdapter, port)
        original = adapter.oauth_transport
        submissions: list[bool] = []

        def malformed(request: Any) -> Any:
            if str(request.url).endswith("/token_endpoint"):
                submissions.append(True)
                return httpx2.Response(200, text="synthetic malformed token response")
            return original.handle_request(request)

        adapter.oauth_transport = httpx2.MockTransport(malformed)
        response = (
            client.post(target) if operation == "rotate" else client.get(target, follow_redirects=False)
        )
        assert response.status_code == 503 and response.json() == {"detail": "UNKNOWN_SIDE_EFFECT"}
        failures = (
            store.query(IdentityProviderEvent)
            .filter_by(operation=operation, outcome="UNKNOWN_SIDE_EFFECT")
            .all()
        )
        assert len(failures) == 1 and failures[0].correlation_id
        replay = client.post(target) if operation == "rotate" else client.get(target, follow_redirects=False)
        assert replay.status_code == 401 and submissions == [True]


@pytest.mark.parametrize("operation", ["complete_login", "authenticate_request"])
def test_unverified_subject_is_never_attributed_to_failed_operation(operation: str) -> None:
    import base64
    import json
    from typing import Any
    from urllib.parse import parse_qs, urlsplit

    import httpx2
    from contracts.test_identity_swappability import identity_shape, login

    from aco.features.identity.models import IdentityProviderEvent, LoginSession, User

    with identity_shape("oidc") as (client, store, _, _, port, grant):
        if operation == "authenticate_request":
            login(client, grant)
            target = "/api/me"
        else:
            started = client.get("/api/auth/login", follow_redirects=False)
            state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
            target = "/api/auth/callback?state=" + state + "&code=" + grant(state)
        sessions_before, users_before = store.query(LoginSession).count(), store.query(User).count()
        original = port.oauth_transport  # type: ignore[attr-defined]
        forged_subject = "unverified-forged-subject"
        provider_calls: list[bool] = []

        def forged(request: Any) -> Any:
            result = original.handle_request(request)
            if str(request.url).endswith("/token_endpoint"):
                provider_calls.append(True)
                body = result.json()
                header, payload, signature = body["id_token"].split(".")
                claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
                claims["sub"] = forged_subject
                payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
                # Retain the original signature: actual maintained verification must reject this.
                body["id_token"] = ".".join((header, payload, signature))
                return httpx2.Response(200, json=body)
            if str(request.url).endswith("/introspection_endpoint"):
                provider_calls.append(True)
                return httpx2.Response(200, json={"active": True, "sub": forged_subject})
            return result

        port.oauth_transport = httpx2.MockTransport(forged)  # type: ignore[attr-defined]
        response = client.get(target, follow_redirects=False)
        assert response.status_code == 401 and response.json() == {"detail": "AUTH_EXPIRED"}
        assert provider_calls == [True]
        store.expire_all()
        rows = store.query(IdentityProviderEvent).filter_by(operation=operation).all()
        assert len(rows) == 1 and rows[0].native_id is None
        assert rows[0].outcome == "AUTH_EXPIRED" and rows[0].attempt == 1
        assert rows[0].correlation_id and rows[0].cost_quality == "UNKNOWN" and rows[0].cost_amount is None
        assert store.query(LoginSession).count() == sessions_before
        assert store.query(User).count() == users_before
        assert store.query(IdentityProviderEvent).filter_by(native_id=forged_subject).count() == 0
