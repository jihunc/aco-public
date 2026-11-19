"""Shared production-consumer contracts: OIDC and independent opaque synthetic grants.

Neither fake transport nor synthetic grants constitute a live second-provider claim.
"""

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx2
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from identity_support import actor_client
from joserfc import jwt
from joserfc.jwk import RSAKey
from sqlalchemy.orm import Session
from synthetic_identity import SyntheticGrantIdentity

from aco.features.identity.models import (
    IdentityProviderEvent,
    LoginAttempt,
    LoginSession,
    Membership,
    Tenant,
    User,
    now,
)
from aco.integrations.ports.identity import IdentityPort


@contextmanager
def identity_shape(
    shape: str,
) -> Iterator[tuple[TestClient, Session, User, str, IdentityPort, Callable[[str], str]]]:
    with (
        actor_client("owner") as (client, store, user, workspace),
        respx.mock(assert_all_called=False) as router,
    ):
        router.get("https://outage.synthetic.invalid/discovery").respond(503)
        app = cast(FastAPI, client.app)
        oidc = app.state.identity
        if shape == "synthetic_grant":
            synthetic = SyntheticGrantIdentity(app.state.business_sessions, user.id, "http://127.0.0.1:53000")
            port: IdentityPort = synthetic
            grant = synthetic.issue_grant
        else:
            port = oidc
            key = RSAKey.generate_key(2048)
            key.ensure_kid()
            router.get(oidc.settings.identity_issuer + "/jwks_uri").respond(
                200, json={"keys": [key.as_dict(private=False)]}
            )
            nonce: list[str] = []

            def grant(state: str) -> str:
                attempt = store.query(LoginAttempt).filter_by(state=state).one()
                nonce[:] = [oidc.reveal(attempt.encrypted_payload)["nonce"]]
                return "explicit-synthetic-code"

            def transport(request: Any) -> Any:
                if str(request.url).endswith("/token_endpoint"):
                    claims = {
                        "iss": user.issuer,
                        "sub": user.subject,
                        "aud": "aco",
                        "iat": int(now().timestamp()),
                        "exp": int(now().timestamp()) + 300,
                        "nonce": nonce[0],
                        "email": user.email,
                        "email_verified": True,
                    }
                    return httpx2.Response(
                        200,
                        json={
                            "access_token": user.subject,
                            "refresh_token": "synthetic-refresh",
                            "id_token": jwt.encode({"alg": "RS256", "kid": key.kid}, claims, key),
                            "expires_in": 300,
                            "token_type": "Bearer",
                        },
                    )
                if str(request.url).endswith("/introspection_endpoint"):
                    return httpx2.Response(200, json={"active": True, "sub": user.subject})
                if str(request.url).endswith("/revocation_endpoint"):
                    return httpx2.Response(200)
                raise AssertionError("Unexpected synthetic OIDC request")

            oidc.oauth_transport = httpx2.MockTransport(transport)
        app.state.ports = replace(app.state.ports, identity=port)
        client.cookies.clear()
        yield client, store, user, str(workspace), port, grant


def login(client: TestClient, grant: Callable[[str], str]) -> str:
    started = client.get("/api/auth/login", follow_redirects=False)
    assert started.status_code == 302
    assert "HttpOnly" in started.headers["set-cookie"]
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    callback = "/api/auth/callback?state=" + state + "&code=" + grant(state)
    response = client.get(callback, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "http://127.0.0.1:53000/onboarding"
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]
    return callback


@pytest.mark.parametrize("shape", ["oidc", "synthetic_grant"])
def test_identity_shapes_shared_login_authority_rotation_logout(shape: str) -> None:
    with identity_shape(shape) as (client, store, user, workspace, port, grant):
        assert asyncio.run(port.validate_connection()) is True
        capabilities = asyncio.run(port.describe_capabilities())
        assert {"server_session", "refresh_rotation", "revocation"} <= capabilities.features
        assert ("pkce_s256" in capabilities.features) == (shape == "oidc")
        assert "send_email" not in capabilities.features
        callback = login(client, grant)
        # No repeated callback may mint another app session.
        count = store.query(LoginSession).count()
        assert client.get(callback, follow_redirects=False).status_code == 401
        assert store.query(LoginSession).count() == count
        me = client.get("/api/me")
        assert me.status_code == 200 and me.json()["id"] == str(user.id)
        client.headers["x-csrf-token"] = me.json()["csrf_token"]
        own = client.get(f"/api/workspaces/{workspace}?role=owner")
        assert own.status_code == 200 and "settings" in own.json()["permissions"]
        tenant_count = store.query(Tenant).count()
        assert client.get(f"/api/workspaces/{uuid4()}?role=owner").status_code == 403
        member = store.query(Membership).filter_by(user_id=user.id).one()
        member.active = False
        store.commit()
        assert client.get(f"/api/workspaces/{workspace}").status_code == 403
        assert client.get("/api/me").json()["workspaces"] == []
        member.active = True
        store.commit()
        old_cookie = client.cookies.get("aco_session")
        rotated = client.post("/api/auth/refresh")
        assert rotated.status_code == 200
        new_cookie = client.cookies.get("aco_session")
        assert new_cookie and new_cookie != old_cookie
        assert client.get("/api/me", headers={"cookie": f"aco_session={old_cookie}"}).status_code == 401
        assert client.get("/api/me").status_code == 200
        client.headers["x-csrf-token"] = rotated.json()["csrf_token"]
        logged_out = client.post("/api/auth/logout")
        assert logged_out.status_code == 200 and logged_out.json()["signed_out"] is True
        assert client.get("/api/me", headers={"cookie": f"aco_session={new_cookie}"}).status_code == 401
        assert store.query(Tenant).count() == tenant_count
        events = store.query(IdentityProviderEvent).all()
        assert {
            "begin_login",
            "complete_login",
            "authenticate_request",
            "rotate",
            "logout",
            "validate_connection",
        } <= {e.operation for e in events}
        provider = user.issuer if shape == "oidc" else "synthetic-grant-provider"
        own_events = [e for e in events if e.provider == provider]
        assert own_events and all(
            e.connection_id and e.correlation_id and e.cost_quality == "UNKNOWN" and e.cost_amount is None
            for e in own_events
        )
        assert any(e.native_id == user.subject and e.operation == "authenticate_request" for e in own_events)


@pytest.mark.parametrize("shape", ["oidc", "synthetic_grant"])
def test_identity_shapes_shared_invalid_session_csrf_expiry_and_outage(shape: str) -> None:
    with identity_shape(shape) as (client, store, user, workspace, port, grant):
        assert client.get("/api/me", headers={"cookie": "aco_session=forged"}).status_code == 401
        login(client, grant)
        csrf = client.get("/api/me").json()["csrf_token"]
        before = store.get(Tenant, workspace)
        assert before is not None
        name = before.name
        client.headers["x-csrf-token"] = "wrong"
        assert (
            client.patch(
                f"/api/workspaces/{workspace}", json={"name": "forged", "timezone": "UTC"}
            ).status_code
            == 403
        )
        client.headers["x-csrf-token"] = csrf
        assert (
            client.patch(
                f"/api/workspaces/{workspace}",
                headers={"origin": "https://attacker.invalid"},
                json={"name": "forged", "timezone": "UTC"},
            ).status_code
            == 403
        )
        store.refresh(before)
        assert before.name == name
        # Same mapped provider failure from different independently controlled shapes.
        if isinstance(port, SyntheticGrantIdentity):
            port.available = False
        else:
            app = cast(FastAPI, client.app)
            app.state.identity.settings = app.state.identity.settings.model_copy(
                update={"identity_discovery_url": "https://outage.synthetic.invalid/discovery"}
            )
        failed = client.get("/api/me")
        assert failed.status_code == 503 and failed.json()["detail"] == "TEMPORARILY_UNAVAILABLE"
        # Local logout remains effective even while provider connection is unavailable.
        cookie = client.cookies.get("aco_session")
        logout = client.post("/api/auth/logout")
        assert logout.status_code == 200 and logout.json()["provider_revoked"] is False
        if isinstance(port, SyntheticGrantIdentity):
            port.available = True
        else:
            app.state.identity.settings = app.state.identity.settings.model_copy(
                update={"identity_discovery_url": user.issuer + "/.well-known/openid-configuration"}
            )
        assert client.get("/api/me", headers={"cookie": f"aco_session={cookie}"}).status_code == 401
        login(client, grant)
        for session in store.query(LoginSession).filter_by(user_id=user.id, revoked=False):
            session.expires_at = now() - timedelta(seconds=1)
        store.commit()
        assert client.get("/api/me").status_code == 401
