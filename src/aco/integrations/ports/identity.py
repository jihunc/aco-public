"""Provider-neutral identity contract. A subject grants no tenant authority."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from starlette.requests import Request
from starlette.responses import Response

from aco.core.types import ProviderCapabilities, ProviderFailure


@dataclass(frozen=True)
class AuthenticatedSubject:
    user_id: UUID
    issuer: str
    subject: str
    session_id: UUID
    authenticated_at: datetime
    correlation_id: UUID


class IdentityFailure(Exception):
    def __init__(self, failure: ProviderFailure):
        self.failure = failure
        super().__init__(failure.code.value)


class IdentityPort(Protocol):
    async def begin_login(self, request: Request) -> Response: ...
    async def complete_login(self, request: Request) -> Response: ...
    async def authenticate_request(self, request: Request) -> AuthenticatedSubject: ...
    async def rotate(self, request: Request) -> Response: ...
    async def logout(self, request: Request) -> Response: ...
    async def validate_connection(self) -> bool: ...
    async def describe_capabilities(self) -> ProviderCapabilities: ...
