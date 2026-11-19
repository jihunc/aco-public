"""Infrastructure-only health adapters, not authenticated business provider ports."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from aco.core.settings import Settings


class DependencyProbe(Protocol):
    async def check(self) -> str: ...


class DatabaseProbe:
    def __init__(self, engine: AsyncEngine | None, timeout: float, configured: bool = False) -> None:
        self.engine = engine
        self.timeout = timeout
        self.configured = configured

    async def check(self) -> str:
        if not self.engine:
            return "UNAVAILABLE" if self.configured else "NOT_CONFIGURED"
        try:
            async with asyncio.timeout(self.timeout):
                async with self.engine.connect() as connection:
                    value = await connection.scalar(text("SELECT 1"))
                    return "READY" if value == 1 else "UNAVAILABLE"
        except Exception:
            return "UNAVAILABLE"


class PrivateStorageProbe:
    def __init__(self, endpoint: str | None, client: httpx.AsyncClient) -> None:
        self.endpoint, self.client = endpoint, client

    async def check(self) -> str:
        if not self.endpoint:
            return "NOT_CONFIGURED"
        try:
            response = await self.client.get(self.endpoint)
            # A private S3 service must reject an anonymous root read.
            return "READY" if response.status_code == 403 else "UNAVAILABLE"
        except (httpx.HTTPError, httpx.InvalidURL):
            return "UNAVAILABLE"


class OIDCDiscoveryProbe:
    def __init__(self, url: str | None, issuer: str | None, client: httpx.AsyncClient) -> None:
        self.url, self.issuer, self.client = url, issuer, client

    async def check(self) -> str:
        if not self.url:
            return "NOT_CONFIGURED"
        try:
            response = await self.client.get(self.url)
            metadata = response.json()
            valid = (
                response.status_code == 200
                and isinstance(metadata, dict)
                and metadata.get("issuer") == self.issuer
                and "S256" in metadata.get("code_challenge_methods_supported", [])
                and all(metadata.get(key) for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"))
            )
            return "READY" if valid else "UNAVAILABLE"
        except (httpx.HTTPError, httpx.InvalidURL, ValueError, TypeError):
            return "UNAVAILABLE"


class WorkerProbe(DatabaseProbe):
    async def check(self) -> str:
        if self.engine is None:
            return "UNAVAILABLE" if self.configured else "NOT_CONFIGURED"
        try:
            async with asyncio.timeout(self.timeout):
                async with self.engine.connect() as connection:
                    alive = await connection.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM procrastinate_workers "
                            "WHERE last_heartbeat >= now() - interval '3 seconds')"
                        )
                    )
                    return "READY" if alive else "UNAVAILABLE"
        except Exception:
            return "UNAVAILABLE"


@asynccontextmanager
async def dependency_probes(settings: Settings) -> AsyncIterator[dict[str, DependencyProbe]]:
    engine = None
    if settings.database_url:
        try:
            engine = create_async_engine(settings.database_url.get_secret_value())
        except Exception:
            # Configuration/driver errors are health failures too, never public DSNs.
            engine = None
    try:
        async with httpx.AsyncClient(
            timeout=settings.health_timeout_seconds, trust_env=False, follow_redirects=False
        ) as client:
            yield {
                "database": DatabaseProbe(
                    engine, settings.health_timeout_seconds, bool(settings.database_url)
                ),
                "storage": PrivateStorageProbe(settings.storage_endpoint, client),
                "identity": OIDCDiscoveryProbe(
                    settings.identity_discovery_url, settings.identity_issuer, client
                ),
                "worker": WorkerProbe(engine, settings.health_timeout_seconds, bool(settings.database_url)),
            }
    finally:
        if engine:
            await engine.dispose()
