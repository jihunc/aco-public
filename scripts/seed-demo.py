"""Explicit local synthetic setup using trusted existing Keycloak identity records.

No session is fabricated; users sign in through the normal OIDC browser flow.
The operator account is created only if missing; existing users/passwords are preserved.
"""

import asyncio
import os
from uuid import UUID

import httpx
from dotenv import dotenv_values
from sqlalchemy import URL, make_url, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aco.core.settings import load_settings
from aco.features.accounting.seed import prepare_demo_tenants, seed_demo_tenants
from aco.features.identity.models import User


def main() -> None:
    values = {k: v for k, v in dotenv_values(".env").items() if v is not None}
    values.update(os.environ)
    local_database = URL.create(
        "postgresql+psycopg",
        username="aco",
        password=values.get("DATABASE_PASSWORD"),
        host="127.0.0.1",
        port=55432,
        database="aco",
    ).render_as_string(hide_password=False)
    settings = load_settings(
        {
            **values,
            "DATABASE_URL": values.get("DATABASE_URL") or local_database,
            "OIDC_ISSUER": values.get("OIDC_ISSUER") or "http://127.0.0.1:58080/realms/aco",
            "OIDC_DISCOVERY_URL": values.get("OIDC_DISCOVERY_URL")
            or "http://127.0.0.1:58080/realms/aco/.well-known/openid-configuration",
        }
    )
    if settings.app_env != "local" or not settings.database_url or not settings.identity_issuer:
        raise SystemExit("Local configured identity/database required")
    if settings.identity_issuer != "http://127.0.0.1:58080/realms/aco":
        raise SystemExit("Only the configured local Keycloak setup is permitted")
    database = make_url(settings.database_url.get_secret_value())
    if (
        database.drivername != "postgresql+psycopg"
        or database.host != "127.0.0.1"
        or database.port != 55432
        or database.database != "aco"
        or database.username != "aco"
        or database.query
    ):
        raise SystemExit("Only the configured local synthetic database is permitted")
    identities: list[dict[str, object]] = []
    with httpx.Client(base_url="http://127.0.0.1:58080", trust_env=False, timeout=20) as client:
        response = client.post(
            "/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": values["KEYCLOAK_ADMIN_USER"],
                "password": values["KEYCLOAK_ADMIN_PASSWORD"],
            },
        )
        response.raise_for_status()
        client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
        for email in ("owner@example.test", "operator@example.test"):
            response = client.get("/admin/realms/aco/users", params={"username": email, "exact": "true"})
            response.raise_for_status()
            users = response.json()
            if not users and email == "operator@example.test":
                response = client.post(
                    "/admin/realms/aco/users",
                    json={
                        "username": email,
                        "email": email,
                        "emailVerified": True,
                        "enabled": True,
                        "firstName": "Synthetic",
                        "lastName": "Operator",
                        "credentials": [
                            {"type": "password", "value": values["DEMO_PASSWORD"], "temporary": False}
                        ],
                    },
                )
                response.raise_for_status()
                response = client.get("/admin/realms/aco/users", params={"username": email, "exact": "true"})
                response.raise_for_status()
                users = response.json()
            if (
                len(users) != 1
                or not users[0].get("enabled")
                or not users[0].get("emailVerified")
                or users[0].get("email") != email
            ):
                raise SystemExit(
                    "Verified local synthetic users required; existing identities left unchanged"
                )
            identities.append(users[0])

    async def seed() -> list[UUID]:
        assert settings.database_url is not None
        engine = create_async_engine(settings.database_url.get_secret_value())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False).begin() as db:
                ids = []
                for item in identities:
                    user = await db.scalar(
                        select(User).where(
                            User.issuer == settings.identity_issuer, User.subject == str(item["id"])
                        )
                    )
                    if user is None:
                        user = User(
                            issuer=settings.identity_issuer,
                            subject=str(item["id"]),
                            email=str(item["email"]),
                            email_verified=True,
                        )
                        db.add(user)
                        await db.flush()
                    ids.append(user.id)
                await prepare_demo_tenants(db, ids[0], ids[1], settings.app_env)
            async with async_sessionmaker(engine, expire_on_commit=False).begin() as db:
                return await seed_demo_tenants(db, ids[0], ids[1], settings.app_env)
        finally:
            await engine.dispose()

    ids = asyncio.run(seed())
    print(
        f"Synthetic A/B seeded: {ids[0]}, {ids[1]}; "
        "DRAFT policies, outbound paused. Normal OIDC sign-in required."
    )


if __name__ == "__main__":
    main()
