"""Explicit runtime injection; default selection never reads credentials or enables live traffic.

The application must supply canonical authority/operation-history and private binding
resolvers. This factory does not override the existing synthetic-only capture/dispatch
services or create a live approval, recipient allowlist, mailbox or provider connection.
"""

from collections.abc import Callable
from uuid import UUID

import httpx

from aco.core.clock import Clock
from aco.core.types import ProviderErrorCode
from aco.integrations.ports.email_observer import EmailRequestObserver
from aco.integrations.resend.adapter import (
    AttachmentResolver,
    ResendAuthority,
    ResendEmailAdapter,
    failure,
)
from aco.integrations.resend.settings import ResendBinding


class ResendRuntime:
    def __init__(
        self,
        binding_resolver: Callable[[UUID, UUID], ResendBinding],
        *,
        authority: ResendAuthority,
        enabled: bool = False,
        send_enabled: bool = False,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
        clock: Clock | None = None,
        attachment_resolver: AttachmentResolver | None = None,
        observer: EmailRequestObserver | None = None,
    ):
        self.binding_resolver, self.authority = binding_resolver, authority
        self.enabled, self.send_enabled = enabled, send_enabled
        self.transport_factory, self.clock = transport_factory, clock
        self.attachment_resolver = attachment_resolver
        self.observer = observer

    def bind(self, tenant_id: UUID, connection_id: UUID) -> ResendEmailAdapter:
        if not self.enabled:
            raise failure(ProviderErrorCode.UNSUPPORTED_CAPABILITY)
        binding = self.binding_resolver(tenant_id, connection_id)
        if (binding.tenant_id, binding.connection_id) != (tenant_id, connection_id):
            raise failure()
        return ResendEmailAdapter(
            binding,
            authority=self.authority,
            enabled=True,
            send_enabled=self.send_enabled,
            transport=self.transport_factory() if self.transport_factory else None,
            clock=self.clock,
            attachment_resolver=self.attachment_resolver,
            observer=self.observer,
        )
