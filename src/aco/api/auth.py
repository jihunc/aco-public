"""Composition dependencies: no caller-supplied role or tenant grants authority."""

from collections.abc import AsyncIterator
from typing import cast

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aco.core.settings import FakePort
from aco.integrations.ports.identity import AuthenticatedSubject, IdentityPort


def identity(request: Request) -> IdentityPort:
    port = request.app.state.ports.identity
    if isinstance(port, FakePort):
        raise HTTPException(503, "TEMPORARILY_UNAVAILABLE")
    return cast(IdentityPort, port)


async def authenticate_request(request: Request) -> AuthenticatedSubject:
    if not request.cookies.get("aco_session"):
        raise HTTPException(401, "AUTH_EXPIRED")
    return await identity(request).authenticate_request(request)


async def database(request: Request) -> AsyncIterator[AsyncSession]:
    sessions = cast(async_sessionmaker[AsyncSession], request.app.state.business_sessions)
    async with sessions.begin() as db:
        yield db
