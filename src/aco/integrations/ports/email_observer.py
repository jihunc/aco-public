"""Optional durable per-wire usage evidence; no vendor URLs, credentials, headers or body.

started must commit intent before returning. finished records each actual HTTP/CDN
attempt independently of business capture commit/rollback. A failed completion write
leaves durable pending evidence and cannot authorize retrying a possible send.
"""

from typing import Protocol

from aco.core.context import ActorContext
from aco.core.types import ProviderFailure
from aco.integrations.ports.email_context import EmailIngressContext, EmailWebhookContext


class EmailRequestObserver(Protocol):
    async def started(
        self,
        ctx: ActorContext | EmailIngressContext | EmailWebhookContext,
        operation: str,
        material: object | None,
        /,
    ) -> object: ...

    async def finished(
        self,
        token: object,
        failure: ProviderFailure | None,
        provider_request_id: str | None,
        status_code: int | None,
        response_bytes: int,
        /,
    ) -> None: ...
