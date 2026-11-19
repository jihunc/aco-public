"""Explicit private operator selection for application email; never reads .env.live.

Selection does not install grants or connections. Every wire/archive still checks current
persisted authority. No default provider selection, external request, or secret logging.
"""

import json
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TypeVar
from uuid import UUID

from pydantic import Field, SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from aco.core.live import DELIVERY_LIVE_AUTHORIZATION
from aco.core.models import register_models
from aco.core.settings import Settings
from aco.core.types import StrictModel
from aco.features.communications.email_live import ApprovedEmailRetention, EmailLiveAuthorization, config_for
from aco.features.communications.models import EmailIngressActivation, EmailMailboxClaim
from aco.features.communications.provider_ingress import (
    Authority,
    ProviderIngressCapability,
    ProviderIngressRuntime,
)
from aco.features.communications.provider_outbound import ProviderOutboundCapability, ProviderOutboundRuntime
from aco.features.identity.models import ProviderConnection, now
from aco.integrations.ports.email import EmailPort
from aco.integrations.ports.email_observer import EmailRequestObserver
from aco.integrations.ports.email_routing import EmailWebhookRoutingPort
from aco.integrations.ports.storage import StorageRuntime
from aco.integrations.provider_smoke.campaign import verify_grant_allocation
from aco.integrations.provider_smoke.source import current_source_commit
from aco.integrations.resend.outbound import compose_outbound
from aco.integrations.resend.routing import ResendWebhookRouter
from aco.integrations.resend.runtime import ResendRuntime
from aco.integrations.resend.settings import ResendBinding


class EmailPrivateConfig(StrictModel):
    grant_id: UUID
    tenant_id: UUID
    connection_id: UUID
    authorization_ref: str = Field(min_length=1, max_length=200)
    source_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    credential_file: Path
    credential_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    download_hosts: tuple[str, ...] = Field(default=(), max_length=8)
    webhook_endpoint_id: UUID | None = None
    kill_switch: bool = True
    campaign_file: Path | None = None
    allocation_id: UUID | None = None


class EmailPrivateCredentials(StrictModel):
    api_key: SecretStr
    webhook_secret: SecretStr | None = None


def read_private(path: Path) -> bytes:
    """One bounded no-follow descriptor, current owner and 0600; errors never echo contents."""
    fd = -1
    try:
        if not path.is_absolute() or path.is_symlink() or path.name == ".env.live":
            raise ValueError()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 32768
        ):
            raise ValueError()
        raw = os.read(fd, 32769)
        if len(raw) > 32768:
            raise ValueError()
        return raw
    except (OSError, ValueError):
        raise ValueError("EMAIL_PRIVATE_CONFIGURATION_REQUIRED") from None
    finally:
        if fd >= 0:
            os.close(fd)


def _parse_private_config(raw: bytes) -> EmailPrivateConfig:
    try:
        config = EmailPrivateConfig.model_validate_json(raw)
        if not config.credential_file.is_absolute():
            raise ValueError()
        return config
    except Exception:
        raise ValueError("EMAIL_PRIVATE_CONFIGURATION_REQUIRED") from None


def load_private_config(path: Path) -> EmailPrivateConfig:
    return _parse_private_config(read_private(path))


@dataclass(frozen=True)
class EmailApplicationRuntime:
    ingress_runtime: ProviderIngressRuntime | None
    outbound_runtime: ProviderOutboundRuntime
    shared_webhooks: Mapping[UUID, EmailWebhookRoutingPort]


R = TypeVar("R")


def local_email_runtime(
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    storage: StorageRuntime | None,
    *,
    config_path: Path | None = None,
) -> EmailApplicationRuntime | None:
    if config_path is None:
        return None
    try:
        if (
            settings.app_env not in {"local", "demo"}
            or settings.kill_switch
            or not settings.outbound_enabled
            or settings.external_providers != "fake"
            or settings.database_url is None
            or storage is None
        ):
            raise ValueError()
        original_config = read_private(config_path)
        config = _parse_private_config(original_config)
        if config.kill_switch or current_source_commit() != config.source_commit:
            raise ValueError()
        # Actual persisted grant is checked BEFORE reading the credential file.
        register_models()
        database_url = settings.database_url.get_secret_value()

        def read(check: Callable[[Session], R]) -> R:
            engine = create_engine(database_url, poolclass=NullPool)
            try:
                with Session(engine) as db:
                    return check(db)
            finally:
                engine.dispose()

        def campaign_check() -> None:
            if config.authorization_ref != DELIVERY_LIVE_AUTHORIZATION:
                return
            if config.campaign_file is None or config.allocation_id is None:
                raise ValueError("LIVE_CAMPAIGN_REQUIRED")
            verify_grant_allocation(
                config.campaign_file,
                config.allocation_id,
                grant_id=config.grant_id,
                provider="resend",
                source_commit=config.source_commit,
                max_requests=material.max_requests,
                max_usd=material.max_usd,
                max_messages=material.max_messages,
            )

        def killed() -> bool:
            try:
                campaign_check()
                return (
                    settings.kill_switch
                    or read_private(config_path) != original_config
                    or sha256(read_private(config.credential_file)).hexdigest() != config.credential_sha256
                    or current_source_commit() != config.source_commit
                )
            except Exception:
                return True

        # Startup validates DB material without the credential-dependent kill callback first.
        startup = EmailLiveAuthorization(
            config.grant_id,
            authorization_ref=config.authorization_ref,
            source_commit=config.source_commit,
            kill_switch=lambda: False,
        )
        grant = read(lambda db: startup.validate(db, config.tenant_id, config.connection_id))
        connection = read(lambda db: db.get(ProviderConnection, grant.storage_connection_id))
        if connection is None or connection.provider != storage.provider:
            raise ValueError()
        material = config_for(grant)
        campaign_check()
        approved_policy = read(
            lambda db: startup.retention(db, config.tenant_id, grant.storage_connection_id)
        )
        approval = ApprovedEmailRetention.model_validate_json(json.dumps(approved_policy.approval))
        raw_credentials = read_private(config.credential_file)
        if sha256(raw_credentials).hexdigest() != config.credential_sha256:
            raise ValueError()
        credentials = EmailPrivateCredentials.model_validate_json(raw_credentials)
        if not credentials.api_key.get_secret_value():
            raise ValueError()
        outbound_only = material.mode == "OUTBOUND_TEST_ONLY"
        if outbound_only:
            if config.webhook_endpoint_id is not None or config.download_hosts or credentials.webhook_secret:
                raise ValueError("OUTBOUND_ONLY_HAS_NO_RECEIVING_AUTHORITY")
            secret_hash = None
        else:
            if (
                credentials.webhook_secret is None
                or config.webhook_endpoint_id is None
                or not config.download_hosts
            ):
                raise ValueError("ROUNDTRIP_RECEIVING_CONFIGURATION_REQUIRED")
            secret_hash = sha256(credentials.webhook_secret.get_secret_value().encode()).hexdigest()
        live = EmailLiveAuthorization(
            config.grant_id,
            authorization_ref=config.authorization_ref,
            source_commit=config.source_commit,
            kill_switch=killed,
        )

        def ingress_cap(tenant_id: UUID, connection_id: UUID) -> ProviderIngressCapability:
            if outbound_only or material.reply_mailbox is None or secret_hash is None:
                raise LookupError("OUTBOUND_ONLY_HAS_NO_RECEIVING_AUTHORITY")
            current = read(lambda db: live.validate(db, tenant_id, connection_id))
            if (tenant_id, connection_id) != (config.tenant_id, config.connection_id):
                raise LookupError()
            return ProviderIngressCapability.model_validate_json(
                json.dumps(
                    {
                        "tenant_id": str(tenant_id),
                        "connection_id": str(connection_id),
                        "credential_revision": current.credential_revision,
                        "inbox_addresses": [material.reply_mailbox],
                        "webhook_secret_sha256": secret_hash,
                        "retention_policy_id": str(current.retention_policy_id),
                        "retention_policy_version": current.retention_policy_version,
                        "capture_purpose": "approved_live_test_capture",
                        "evidence_mode": "LIVE",
                        "live_grant_id": str(current.id),
                    }
                )
            )

        def binding(tenant_id: UUID, connection_id: UUID) -> ResendBinding:
            read(lambda db: live.validate(db, tenant_id, connection_id))
            current_raw = read_private(config.credential_file)
            if sha256(current_raw).hexdigest() != config.credential_sha256:
                raise ValueError("EMAIL_CREDENTIAL_CHANGED")
            private = EmailPrivateCredentials.model_validate_json(current_raw)
            return ResendBinding(
                tenant_id=tenant_id,
                connection_id=connection_id,
                api_key=private.api_key,
                inbox_addresses=() if material.reply_mailbox is None else (material.reply_mailbox,),
                sender_addresses=(material.sender,) if outbound_only else (),
                download_hosts=frozenset(config.download_hosts),
                webhook_secret=private.webhook_secret,
                evidence_mode="LIVE",
                max_source_bytes=approval.max_bytes,
            )

        def ingress_port(
            cap: ProviderIngressCapability, authority: Authority, observer: EmailRequestObserver
        ) -> EmailPort:
            return ResendRuntime(
                lambda tenant, key: binding(tenant, key),
                authority=authority,
                observer=observer,
                enabled=True,
                send_enabled=False,
            ).bind(cap.tenant_id, cap.connection_id)

        ingress = (
            None
            if outbound_only
            else ProviderIngressRuntime(
                sessions,
                ingress_cap,
                ingress_port,
                enabled=True,
                environment=settings.app_env,
                live_authorization=live,
            )
        )

        def outbound_cap(tenant_id: UUID, connection_id: UUID) -> ProviderOutboundCapability:
            current = read(lambda db: live.validate(db, tenant_id, connection_id))

            def current_activation(db: Session) -> UUID:
                rows = list(
                    db.scalars(
                        select(EmailIngressActivation)
                        .join(EmailMailboxClaim, EmailMailboxClaim.activation_id == EmailIngressActivation.id)
                        .where(
                            EmailMailboxClaim.address == material.reply_mailbox,
                            EmailMailboxClaim.released_at.is_(None),
                            EmailIngressActivation.tenant_id == tenant_id,
                            EmailIngressActivation.email_connection_id == connection_id,
                            EmailIngressActivation.revoked_at.is_(None),
                            EmailIngressActivation.expires_at > now(),
                        )
                    )
                )
                if (
                    len(rows) != 1
                    or rows[0].provider_binding is None
                    or rows[0].provider_binding.get("live_grant_id") != str(current.id)
                ):
                    raise LookupError("CURRENT_LIVE_INGRESS_ACTIVATION_REQUIRED")
                return rows[0].id

            return ProviderOutboundCapability(
                tenant_id=tenant_id,
                connection_id=connection_id,
                credential_revision=current.credential_revision,
                account_id=current.account_id,
                mode=material.mode,
                ingress_activation_id=None if outbound_only else read(current_activation),
                sender=material.sender,
                destinations=(material.recipient,),
                request_cap=material.max_messages,
                observe_sent_identity=True,
                evidence_mode="LIVE",
                live_grant_id=current.id,
            )

        outbound = compose_outbound(
            sessions,
            settings,
            ingress,
            outbound_cap,
            lambda cap: binding(cap.tenant_id, cap.connection_id),
            enabled=True,
            live_authorization=live,
        )
        routes: dict[UUID, EmailWebhookRoutingPort] = {}
        if not outbound_only:
            assert config.webhook_endpoint_id is not None and credentials.webhook_secret is not None
            router = ResendWebhookRouter(
                endpoint_id=config.webhook_endpoint_id,
                account_id=grant.account_id,
                secret=credentials.webhook_secret,
            )
            routes[router.endpoint_id] = router
        return EmailApplicationRuntime(ingress, outbound, routes)
    except Exception:
        raise RuntimeError("BOUNDED_EMAIL_CONFIGURATION_BLOCKED") from None
