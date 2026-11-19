"""Synthetic issuer transport + real PostgreSQL/migrations/app routes; never proof of real login."""

import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import respx
from alembic.config import Config
from cryptography.fernet import Fernet
from dotenv import dotenv_values
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import URL, create_engine, text
from sqlalchemy.orm import Session

from aco.api.app import create_app
from aco.core.settings import Settings
from aco.features.identity.models import LoginSession, Membership, ProviderConnection, Tenant, User, now
from alembic import command


def database_url() -> str:
    values = dotenv_values(".env")
    return os.environ.get("ACO_TEST_DATABASE_URL") or URL.create(
        "postgresql+psycopg",
        username="aco",
        password=values["DATABASE_PASSWORD"],
        host="127.0.0.1",
        port=55432,
        database="aco",
    ).render_as_string(hide_password=False)


def add_actor(store: Session, app: FastAPI, email: str) -> tuple[User, TestClient]:
    user = User(
        issuer=app.state.settings.identity_issuer, subject=str(uuid4()), email=email, email_verified=True
    )
    store.add(user)
    store.flush()
    port = app.state.identity
    session = LoginSession(
        user_id=user.id,
        csrf_token=secrets.token_urlsafe(32),
        encrypted_token=port.protect(
            {
                "access_token": user.subject,
                "refresh_token": "synthetic-refresh",
                "expires_at": now().timestamp() + 300,
            }
        ),
        expires_at=now() + timedelta(hours=1),
    )
    store.add(session)
    store.commit()
    client = TestClient(app, base_url="http://127.0.0.1:53000")
    client.cookies.set("aco_session", port.signer.dumps(str(session.id)))
    client.headers.update({"origin": "http://127.0.0.1:53000", "x-csrf-token": session.csrf_token})
    return user, client


@contextmanager
def actor_client(role: str) -> Iterator[tuple[TestClient, Session, User, UUID]]:
    base = create_engine(database_url())
    schema = "t03_" + uuid4().hex
    with base.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA {schema}"))
    url = base.url.update_query_dict({"options": f"-csearch_path={schema}"})
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%"))
    command.upgrade(config, "head")
    issuer = "https://identity.synthetic.invalid/realms/aco"
    settings = Settings(
        database_url=SecretStr(url.render_as_string(hide_password=False)),
        identity_issuer=issuer,
        identity_discovery_url=issuer + "/.well-known/openid-configuration",
        identity_client_secret=SecretStr("synthetic-secret"),
        session_secret=SecretStr(secrets.token_urlsafe(32)),
        token_encryption_key=SecretStr(Fernet.generate_key().decode()),
    )
    app = create_app(settings)
    from urllib.parse import parse_qs

    import httpx2

    def oauth_response(request: Any) -> Any:
        if str(request.url).endswith("/introspection_endpoint"):
            token = parse_qs(request.content.decode())["token"][0]
            return httpx2.Response(200, json={"active": True, "sub": token})
        if str(request.url).endswith("/revocation_endpoint"):
            return httpx2.Response(200)
        raise AssertionError("Unexpected synthetic identity request")

    app.state.identity.oauth_transport = httpx2.MockTransport(oauth_response)
    try:
        with app.state.sessions() as store, respx.mock(assert_all_called=False) as router:
            metadata = {
                "issuer": issuer,
                "code_challenge_methods_supported": ["S256"],
                **{
                    name: issuer + "/" + name
                    for name in [
                        "authorization_endpoint",
                        "token_endpoint",
                        "jwks_uri",
                        "introspection_endpoint",
                        "revocation_endpoint",
                    ]
                },
            }
            router.get(issuer + "/.well-known/openid-configuration").respond(200, json=metadata)

            def introspect(request: httpx.Request) -> httpx.Response:
                from urllib.parse import parse_qs

                token = parse_qs(request.content.decode())["token"][0]
                return httpx.Response(200, json={"active": True, "sub": token})

            router.post(issuer + "/introspection_endpoint").mock(side_effect=introspect)
            router.post(issuer + "/revocation_endpoint").respond(200)
            user, client = add_actor(store, app, "owner@example.test")
            workspace = Tenant(name="Synthetic", timezone="UTC")
            store.add(workspace)
            store.flush()
            store.add(Membership(tenant_id=workspace.id, user_id=user.id, role=role))
            store.add(ProviderConnection(tenant_id=workspace.id, provider="demo", provider_type="accounting"))
            store.commit()
            yield client, store, user, workspace.id
    finally:
        app.state.sessions.kw["bind"].dispose()
        with base.begin() as conn:
            conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        base.dispose()
