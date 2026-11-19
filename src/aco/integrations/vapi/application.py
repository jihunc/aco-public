"""Explicit private Voice selection; never installs authority or starts a provider request.

A retained verifier may survive action revocation/expiry for exact terminal attribution.
Every new wire, setup, join or business action still requires current action authority.
"""

import os
import re
import stat
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import TypeVar
from uuid import UUID

import httpx
from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, field_validator
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from aco.core.live import DELIVERY_LIVE_AUTHORIZATION
from aco.core.models import register_models
from aco.core.settings import Settings
from aco.core.types import StrictModel
from aco.features.identity.models import ProviderConnection
from aco.features.voice.live import (
    VoiceLiveAuthorization,
    VoiceLiveConfig,
    VoiceLiveGrant,
    VoiceLiveProfile,
    config_for,
    current_grant,
)
from aco.features.voice.live_runtime import LiveVoiceRuntime
from aco.features.voice.runtime import VoiceRuntime
from aco.integrations.provider_smoke.campaign import verify_grant_allocation
from aco.integrations.provider_smoke.source import current_source_commit
from aco.integrations.vapi.settings import VapiBinding

ERROR = "VOICE_PRIVATE_CONFIGURATION_REQUIRED"


class VoicePrivateConfig(StrictModel):
    grant_id: UUID
    tenant_id: UUID
    connection_id: UUID
    authorization_ref: str
    source_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    credential_file: Path
    credential_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    campaign_file: Path
    allocation_id: UUID
    control_hosts: tuple[str, ...] = Field(min_length=1, max_length=4)
    kill_switch: bool = True

    @field_validator("control_hosts")
    @classmethod
    def provider_control_hosts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values) or any(
            len(value) > 253 or re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+vapi\.ai", value) is None
            for value in values
        ):
            raise ValueError(ERROR)
        return values


class VoicePrivateCredentials(StrictModel):
    api_key: SecretStr
    webhook_secret: SecretStr
    control_api_key: SecretStr
    capability_secret: SecretStr
    join_encryption_key: SecretStr


def read_private(path: Path) -> bytes:
    descriptor = -1
    try:
        if not path.is_absolute() or path.is_symlink() or path.name == ".env.live":
            raise ValueError()
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 32768
        ):
            raise ValueError()
        raw = os.read(descriptor, 32769)
        if len(raw) > 32768:
            raise ValueError()
        return raw
    except (OSError, ValueError):
        raise ValueError(ERROR) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def parse_config(raw: bytes) -> VoicePrivateConfig:
    try:
        config = VoicePrivateConfig.model_validate_json(raw)
        if (
            config.authorization_ref != DELIVERY_LIVE_AUTHORIZATION
            or not config.credential_file.is_absolute()
            or not config.campaign_file.is_absolute()
        ):
            raise ValueError()
        return config
    except Exception:
        raise ValueError(ERROR) from None


def retained_binding(db: Session, config: VoicePrivateConfig) -> tuple[VoiceLiveGrant, bool]:
    """Read-only original binding, never current action/member permission or a wire grant."""
    grant = db.get(VoiceLiveGrant, config.grant_id, populate_existing=True)
    connection = db.get(ProviderConnection, config.connection_id, populate_existing=True)
    profile = db.scalar(select(VoiceLiveProfile).where(VoiceLiveProfile.grant_id == config.grant_id))
    if grant is None or connection is None:
        raise ValueError(ERROR)
    material = config_for(grant)
    if (
        (grant.tenant_id, grant.connection_id) != (config.tenant_id, config.connection_id)
        or (material.authorization_ref, material.source_commit)
        != (config.authorization_ref, config.source_commit)
        or connection.tenant_id != config.tenant_id
        or connection.provider != "vapi"
        or connection.provider_type != "voice"
        or connection.external_account_id != material.account_id
        or connection.encrypted_credentials_ref != grant.credential_revision
        or connection.version != (profile.connection_version if profile else grant.connection_version)
        or connection.status != ("READY" if profile else "NOT_CONFIGURED")
        or (
            profile is not None
            and (
                profile.tenant_id != config.tenant_id
                or profile.binding_sha256 != material.binding_sha256
                or profile.assistant_profile_sha256 != material.assistant_profile_sha256
            )
        )
    ):
        raise ValueError(ERROR)
    if profile is None:
        # An unprobed selection still needs current Owner/source/action authority.
        current_grant(db, grant.id, config.tenant_id, config.connection_id, require_ready=False)
    return grant, profile is not None


R = TypeVar("R")


def local_voice_runtime(
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    *,
    config_path: Path | None = None,
) -> VoiceRuntime | None:
    if config_path is None:
        return None
    try:
        if (
            settings.app_env not in {"local", "demo"}
            or settings.external_providers != "fake"
            or settings.database_url is None
        ):
            raise ValueError()
        # Parse the SAME bounded bytes that establish the immutable startup baseline.
        original = read_private(config_path)
        config = parse_config(original)
        if current_source_commit() != config.source_commit:
            raise ValueError()
        register_models()
        url = settings.database_url.get_secret_value()

        def read(check: Callable[[Session], R]) -> R:
            engine = create_engine(url, poolclass=NullPool)
            try:
                with Session(engine) as db:
                    return check(db)
            finally:
                engine.dispose()

        # Persisted original authority precedes any credential-file read. Retained
        # profile construction permits ONLY later exact-native terminal verification.
        grant, retained = read(lambda db: retained_binding(db, config))
        material: VoiceLiveConfig = config_for(grant)
        if not retained and (
            settings.kill_switch
            or not settings.outbound_enabled
            or config.kill_switch
            or settings.synthetic_voice_enabled
        ):
            raise ValueError()

        def campaign_check() -> None:
            verify_grant_allocation(
                config.campaign_file,
                config.allocation_id,
                grant_id=grant.id,
                provider="vapi",
                source_commit=config.source_commit,
                max_requests=material.max_requests,
                max_usd=material.max_usd,
                max_sessions=material.max_calls,
                max_seconds=material.max_total_seconds,
            )

        campaign_check()
        raw_credentials = read_private(config.credential_file)
        if sha256(raw_credentials).hexdigest() != config.credential_sha256:
            raise ValueError()
        credentials = VoicePrivateCredentials.model_validate_json(raw_credentials)
        if (
            not all(
                value.get_secret_value()
                for value in (credentials.api_key, credentials.webhook_secret, credentials.control_api_key)
            )
            or len(credentials.capability_secret.get_secret_value()) < 32
        ):
            raise ValueError()
        Fernet(credentials.join_encryption_key.get_secret_value().encode())
        binding = VapiBinding(
            tenant_id=grant.tenant_id,
            connection_id=grant.connection_id,
            assistant_id=material.assistant_id,
            server_credential_id=material.server_credential_id,
            credential_revision=grant.credential_revision,
            api_key=credentials.api_key,
            webhook_secret=credentials.webhook_secret,
            control_api_key=credentials.control_api_key,
            control_hosts=config.control_hosts,
            evidence_mode="LIVE",
            max_duration_seconds=material.max_call_seconds,
        )
        if VoiceRuntime.binding_hash(binding) != material.binding_sha256:
            raise ValueError()
        baseline = config.model_dump(exclude={"kill_switch"})

        def current_config() -> VoicePrivateConfig:
            current = parse_config(read_private(config_path))
            if (
                current.model_dump(exclude={"kill_switch"}) != baseline
                or current_source_commit() != config.source_commit
                or sha256(read_private(config.credential_file)).hexdigest() != config.credential_sha256
            ):
                raise ValueError(ERROR)
            campaign_check()
            return current

        def integrity() -> bool:
            try:
                current_config()
                return True
            except Exception:
                return False

        def killed() -> bool:
            try:
                return (
                    current_config().kill_switch
                    or settings.kill_switch
                    or not settings.outbound_enabled
                    or settings.synthetic_voice_enabled
                )
            except Exception:
                return True

        if not integrity():
            raise ValueError()
        authority = VoiceLiveAuthorization(
            grant.id,
            authorization_ref=config.authorization_ref,
            source_commit=config.source_commit,
            kill_switch=killed,
            integrity_check=integrity,
        )
        return LiveVoiceRuntime(
            sessions,
            binding,
            authorization=authority,
            capability_secret=credentials.capability_secret,
            join_encryption_key=credentials.join_encryption_key,
            transport_factory=lambda: httpx.AsyncHTTPTransport(retries=0),
        )
    except Exception:
        raise ValueError(ERROR) from None
