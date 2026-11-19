"""Explicit environment mapping; credentials never select live providers implicitly."""

from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from aco.core.types import ProviderCapabilities
from aco.integrations.demo_accounting.adapter import AccountingRuntime
from aco.integrations.demo_email.adapter import DemoEmailRuntime
from aco.integrations.ports.identity import IdentityPort
from aco.integrations.ports.storage import StorageRuntime

if TYPE_CHECKING:
    from aco.features.communications.provider_outbound import ProviderOutboundRuntime


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    app_env: Literal["local", "demo", "production"] = "local"
    external_providers: Literal["fake", "live"] = "fake"
    accounting_adapter: Literal["demo", "fake"] = "demo"
    email_adapter: Literal["mock", "resend"] = "mock"
    voice_adapter: Literal["mock", "vapi"] = "mock"
    llm_provider: Literal["fake", "openai"] = "fake"
    test_profile: Literal["mock", "deployed"] = "mock"
    deployed_origin: str | None = None
    # Selector metadata only; the trusted startup loader still validates all authority.
    voice_live_config_path: Path | None = Field(default=None, repr=False)
    email_live_config_path: Path | None = Field(default=None, repr=False)
    synthetic_voice_enabled: bool = False
    synthetic_email_enabled: bool = False
    synthetic_email_ingress_enabled: bool = False
    synthetic_email_ingress_webhook_secret: SecretStr | None = Field(default=None, repr=False)
    outbound_enabled: bool = False
    kill_switch: bool = True
    recording_enabled: bool = False
    transcription_enabled: bool = False
    database_url: SecretStr | None = Field(default=None, repr=False)
    storage_endpoint: str | None = None
    storage_local_endpoint_ip: str | None = None
    storage_bucket: str | None = None
    storage_access_key: SecretStr | None = Field(default=None, repr=False)
    storage_secret_key: SecretStr | None = Field(default=None, repr=False)
    identity_discovery_url: str | None = None
    identity_issuer: str | None = None
    web_public_url: str = "http://127.0.0.1:53000"
    identity_client_id: str = "aco"
    identity_client_secret: SecretStr | None = Field(default=None, repr=False)
    session_secret: SecretStr | None = Field(default=None, repr=False, min_length=32)
    token_encryption_key: SecretStr | None = Field(default=None, repr=False)
    session_ttl_seconds: int = Field(default=28800, ge=60, le=86400)
    health_timeout_seconds: float = Field(default=2, gt=0, le=10)

    @model_validator(mode="after")
    def fail_closed(self) -> "Settings":
        if self.email_live_config_path is not None and (
            self.app_env not in {"local", "demo"} or not self.email_live_config_path.is_absolute()
        ):
            raise ValueError("Explicit email selection requires an absolute path in local/demo mode")
        if self.voice_live_config_path is not None and (
            self.app_env not in {"local", "demo"} or not self.voice_live_config_path.is_absolute()
        ):
            raise ValueError("Explicit voice selection requires an absolute path in local/demo mode")
        if self.synthetic_voice_enabled and self.app_env not in {"local", "demo"}:
            raise ValueError("Synthetic voice requires explicit local/demo mode")
        if self.synthetic_email_ingress_enabled and (
            self.app_env not in {"local", "demo"}
            or self.synthetic_email_ingress_webhook_secret is None
            or not self.synthetic_email_ingress_webhook_secret.get_secret_value().strip()
        ):
            raise ValueError("Synthetic ingress requires local/demo mode and a private webhook secret")
        if (
            self.external_providers != "fake"
            or self.email_adapter != "mock"
            or self.voice_adapter != "mock"
            or self.llm_provider != "fake"
            or (
                (self.outbound_enabled or not self.kill_switch)
                and not (
                    self.app_env in {"local", "demo"}
                    and (
                        self.synthetic_email_enabled
                        or self.voice_live_config_path is not None
                        or self.email_live_config_path is not None
                    )
                )
            )
            or self.recording_enabled
            or self.transcription_enabled
        ):
            raise ValueError(
                "Live activation requires the separately authorized live runner; unavailable in T02"
            )
        if self.test_profile == "deployed" and not (self.deployed_origin or "").startswith("https://"):
            raise ValueError("Deployed smoke requires an explicit HTTPS origin")
        if self.storage_local_endpoint_ip:
            address = IPv4Address(self.storage_local_endpoint_ip)
            endpoint = urlsplit(self.storage_endpoint or "")
            if (
                self.app_env != "local"
                or not address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_multicast
                or address.is_unspecified
                or (endpoint.scheme, endpoint.hostname, endpoint.port) != ("http", str(address), 8333)
            ):
                raise ValueError("Verified local storage topology does not match endpoint")
        if bool(self.identity_discovery_url) != bool(self.identity_issuer):
            raise ValueError("Identity readiness requires discovery URL and expected issuer together")
        origin = urlsplit(self.web_public_url)
        if (
            origin.scheme not in {"http", "https"}
            or not origin.netloc
            or origin.username
            or origin.password
            or origin.path
            or origin.query
            or origin.fragment
        ):
            raise ValueError("Web public URL must be a bare HTTP(S) origin")
        if self.app_env != "local" and any(
            url and urlsplit(url).scheme != "https"
            for url in (self.web_public_url, self.identity_issuer, self.identity_discovery_url)
        ):
            raise ValueError("Non-local identity requires HTTPS")
        return self


ENV_FIELDS = {
    "APP_ENV": "app_env",
    "ACO_VOICE_LIVE_CONFIG_PATH": "voice_live_config_path",
    "ACO_EMAIL_LIVE_CONFIG_PATH": "email_live_config_path",
    "EXTERNAL_PROVIDERS": "external_providers",
    "ACCOUNTING_ADAPTER": "accounting_adapter",
    "EMAIL_ADAPTER": "email_adapter",
    "VOICE_ADAPTER": "voice_adapter",
    "LLM_PROVIDER": "llm_provider",
    "ACO_TEST_PROFILE": "test_profile",
    "ACO_DEPLOYED_ORIGIN": "deployed_origin",
    "SYNTHETIC_VOICE_ENABLED": "synthetic_voice_enabled",
    "SYNTHETIC_EMAIL_ENABLED": "synthetic_email_enabled",
    "SYNTHETIC_EMAIL_INGRESS_ENABLED": "synthetic_email_ingress_enabled",
    "SYNTHETIC_EMAIL_INGRESS_WEBHOOK_SECRET": "synthetic_email_ingress_webhook_secret",
    "OUTBOUND_ENABLED": "outbound_enabled",
    "AUTOMATION_KILL_SWITCH": "kill_switch",
    "RECORDING_ENABLED": "recording_enabled",
    "TRANSCRIPTION_ENABLED": "transcription_enabled",
    "DATABASE_URL": "database_url",
    "STORAGE_ENDPOINT": "storage_endpoint",
    "ACO_LOCAL_S3_IP": "storage_local_endpoint_ip",
    "STORAGE_BUCKET": "storage_bucket",
    "STORAGE_APP_ACCESS_KEY": "storage_access_key",
    "STORAGE_APP_SECRET_KEY": "storage_secret_key",
    "OIDC_DISCOVERY_URL": "identity_discovery_url",
    "OIDC_ISSUER": "identity_issuer",
    "WEB_PUBLIC_URL": "web_public_url",
    "OIDC_CLIENT_ID": "identity_client_id",
    "OIDC_CLIENT_SECRET": "identity_client_secret",
    "SESSION_SECRET": "session_secret",
    "TOKEN_ENCRYPTION_KEY": "token_encryption_key",
    "SESSION_TTL_SECONDS": "session_ttl_seconds",
    "HEALTH_TIMEOUT_SECONDS": "health_timeout_seconds",
}


def load_settings(env: Mapping[str, str]) -> Settings:
    return Settings.model_validate({field: env[key] for key, field in ENV_FIELDS.items() if env.get(key)})


@dataclass(frozen=True)
class FakePort:
    """Unimplemented family placeholder; no fabricated business operation support."""

    family: str

    def describe_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()


@dataclass(frozen=True)
class PortRegistry:
    accounting: FakePort | AccountingRuntime
    email: "FakePort | DemoEmailRuntime | ProviderOutboundRuntime"
    voice: FakePort
    llm: FakePort
    storage: FakePort | StorageRuntime
    identity: FakePort | IdentityPort
    telephony: FakePort

    def provider_modes(self) -> dict[str, str]:
        return {
            family: "fake" if isinstance(getattr(self, family), FakePort) else "configured"
            for family in self.__dataclass_fields__
        }


def resolve_ports(settings: Settings) -> PortRegistry:
    # Revalidate even a model_copy(update=...) value before composing anything.
    Settings.model_validate(settings.model_dump())
    ports = PortRegistry(*(FakePort(family) for family in PortRegistry.__dataclass_fields__))
    if (
        settings.storage_endpoint
        and settings.storage_bucket
        and settings.storage_access_key
        and settings.storage_secret_key
    ):
        from dataclasses import replace

        from aco.integrations.s3.adapter import StorageRuntime as S3Runtime

        ports = replace(
            ports,
            storage=S3Runtime(
                endpoint=settings.storage_endpoint,
                local_endpoint_ip=settings.storage_local_endpoint_ip,
                bucket=settings.storage_bucket,
                access_key=settings.storage_access_key,
                secret_key=settings.storage_secret_key,
            ),
        )
    return ports
