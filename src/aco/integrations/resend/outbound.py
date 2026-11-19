"""Contained application composition, distinct from any future approved live send grant."""

from collections.abc import Callable

import httpx
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aco.core.settings import Settings
from aco.features.communications.email_live import EmailLiveAuthorization
from aco.features.communications.provider_ingress import Authority, ProviderIngressRuntime
from aco.features.communications.provider_outbound import (
    CapabilityResolver,
    ProviderOutboundCapability,
    ProviderOutboundRuntime,
)
from aco.integrations.ports.email import EmailPort
from aco.integrations.ports.email_observer import EmailRequestObserver
from aco.integrations.resend.runtime import ResendRuntime
from aco.integrations.resend.settings import ResendBinding


def compose_outbound(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    ingress_runtime: ProviderIngressRuntime | None,
    capability_resolver: CapabilityResolver,
    binding_resolver: Callable[[ProviderOutboundCapability], ResendBinding],
    *,
    transport_factory: Callable[[], httpx.MockTransport] | None = None,
    enabled: bool = False,
    live_authorization: EmailLiveAuthorization | None = None,
) -> ProviderOutboundRuntime:
    def port(
        cap: ProviderOutboundCapability, authority: Authority, observer: EmailRequestObserver
    ) -> EmailPort:
        transport = transport_factory() if transport_factory is not None else None
        if cap.evidence_mode == "MOCKED_HTTP":
            if not isinstance(transport, httpx.MockTransport):
                raise HTTPException(403, "MOCKED_EMAIL_TRANSPORT_REQUIRED")
        elif live_authorization is None or cap.live_grant_id != live_authorization.grant_id:
            raise HTTPException(403, "REAL_EMAIL_SEND_NOT_AUTHORIZED")
        # Capability and transport are checked before resolving server-private credentials.
        binding = binding_resolver(cap)
        if (
            binding.evidence_mode != cap.evidence_mode
            or (binding.tenant_id, binding.connection_id) != (cap.tenant_id, cap.connection_id)
            or cap.sender not in (binding.sender_addresses or binding.inbox_addresses)
        ):
            raise HTTPException(403, "PROVIDER_OUTBOUND_BINDING_INVALID")
        return ResendRuntime(
            lambda tenant, key: binding,
            authority=authority,
            observer=observer,
            enabled=True,
            send_enabled=True,
            transport_factory=(lambda: transport) if transport is not None else None,
        ).bind(cap.tenant_id, cap.connection_id)

    return ProviderOutboundRuntime(
        sessions,
        settings,
        ingress_runtime,
        capability_resolver,
        port,
        enabled=enabled,
        live_authorization=live_authorization,
    )
