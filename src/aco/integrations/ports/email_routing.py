"""Optional account-level signed notification routing before tenant selection."""

from collections.abc import Mapping
from typing import Annotated, Protocol
from uuid import UUID

from pydantic import Field

from aco.core.types import StrictModel


class VerifiedEmailRoute(StrictModel):
    event_id: Annotated[str, Field(min_length=1, max_length=200)]
    message_id: Annotated[str, Field(min_length=1, max_length=240)]
    recipients: Annotated[tuple[str, ...], Field(min_length=1, max_length=50)]
    sender: str | None = None
    raw_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class EmailWebhookRoutingPort(Protocol):
    provider: str
    endpoint_id: UUID
    account_id: str
    secret_sha256: str

    async def verify_route(self, raw: bytes, headers: Mapping[str, str]) -> VerifiedEmailRoute: ...
