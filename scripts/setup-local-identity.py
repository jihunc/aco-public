"""Explicit local Keycloak configuration update; preserves users, sessions, volumes and unrelated settings."""

import json
from pathlib import Path

import httpx
from dotenv import dotenv_values

values = dotenv_values(".env")
realm = json.loads(Path("infra/identity/realm.json").read_text())
base = "http://127.0.0.1:58080"
with httpx.Client(trust_env=False, timeout=20) as client:
    response = client.post(
        base + "/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": values["KEYCLOAK_ADMIN_USER"],
            "password": values["KEYCLOAK_ADMIN_PASSWORD"],
        },
    )
    response.raise_for_status()
    client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
    response = client.get(base + "/admin/realms/aco/clients", params={"clientId": "aco"})
    response.raise_for_status()
    current = response.json()[0]
    target = realm["clients"][0]
    current["redirectUris"] = sorted(set(current.get("redirectUris", []) + target["redirectUris"]))
    current["webOrigins"] = sorted(set(current.get("webOrigins", []) + target["webOrigins"]))
    current["protocolMappers"] = [
        m for m in current.get("protocolMappers", []) if m["name"] != "aco-audience"
    ] + target["protocolMappers"]
    response = client.put(base + "/admin/realms/aco/clients/" + current["id"], json=current)
    response.raise_for_status()
    response = client.put(
        base + "/admin/realms/aco",
        json={k: realm[k] for k in ("registrationAllowed", "revokeRefreshToken", "refreshTokenMaxReuse")},
    )
    response.raise_for_status()
print(
    "Local identity audience/callback/registration/rotation configuration updated; existing users preserved."
)
