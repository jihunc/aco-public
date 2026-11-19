"""Private local configuration selects existing adapters; it never grants authority."""

import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from uuid import UUID

if TYPE_CHECKING:
    from aco.core.settings import Settings

ERROR = "DEVELOPMENT_CONFIGURATION_BLOCKED"
SELECTORS = (
    "ACO_AGENT_LIVE_CONFIG_PATH",
    "ACO_AGENT_LIVE_GRANT_ID",
    "ACO_LIVE_AUTHORIZATION_REF",
    "ACO_LIVE_CAMPAIGN_FILE",
    "ACO_LIVE_ALLOCATION_ID",
    "ACO_EMAIL_LIVE_CONFIG_PATH",
    "ACO_VOICE_LIVE_CONFIG_PATH",
)
ALLOWED = {
    "DEV_MODE",
    "DEV_INFRA_CONFIG",
    "DEV_PROVIDER_DIRECTORY",
    "DEV_CAMPAIGN_DIRECTORY",
    "APP_ENV",
    "EXTERNAL_PROVIDERS",
    "LLM_PROVIDER",
    "EMAIL_ADAPTER",
    "VOICE_ADAPTER",
    "ACCOUNTING_ADAPTER",
    "ACO_DELIVERY_APPROVED_EMAIL_RECIPIENT",
    "OUTBOUND_ENABLED",
    "AUTOMATION_KILL_SWITCH",
    *SELECTORS,
}


def private_values(path: Path) -> dict[str, str]:
    """Bounded owned no-follow read; no ambient expansion, duplicate keys or secret errors."""
    descriptor = -1
    try:
        if not path.is_absolute() or path.is_symlink():
            raise ValueError(ERROR)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 32768
        ):
            raise ValueError(ERROR)
        content = os.read(descriptor, 32769).decode("utf-8")
        if len(content.encode()) > 32768 or "${" in content or "\x00" in content:
            raise ValueError(ERROR)
        values: dict[str, str] = {}
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, raw = line.partition("=")
            key, raw = key.strip(), raw.strip()
            if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in values:
                raise ValueError(ERROR)
            value = json.loads(raw) if raw.startswith('"') else raw
            if not isinstance(value, str) or any(char in value for char in "\n\r\x00$`"):
                raise ValueError(ERROR)
            values[key] = value
        return values
    except Exception:
        raise ValueError(ERROR) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


@dataclass(frozen=True)
class DevelopmentConfig:
    mode: str
    llm: str
    email: str
    voice: str
    project: str
    web_port: int
    api_port: int
    identity_port: int
    provider_directory: Path = field(repr=False)
    campaign_directory: Path = field(repr=False)
    path: Path = field(repr=False)
    infra_path: Path = field(repr=False)
    values: Mapping[str, str] = field(repr=False)
    infrastructure: Mapping[str, str] = field(repr=False)

    def selection(self) -> dict[str, str | None]:
        return {key: self.values.get(key) or None for key in SELECTORS}

    def backend_environment(self) -> dict[str, str]:
        infra = self.infrastructure
        result = {
            "APP_ENV": "local",
            "EXTERNAL_PROVIDERS": "fake",
            "ACCOUNTING_ADAPTER": "demo",
            "EMAIL_ADAPTER": "mock",
            "VOICE_ADAPTER": "mock",
            "LLM_PROVIDER": "fake",
            "DATABASE_URL": infra["PRODUCT_DATABASE_URL"],
            "WEB_PUBLIC_URL": f"http://127.0.0.1:{self.web_port}",
            "OIDC_ISSUER": f"http://identity.aco.localhost:{self.identity_port}/realms/aco",
            "OIDC_DISCOVERY_URL": (
                f"http://identity.aco.localhost:{self.identity_port}/realms/aco/.well-known/openid-configuration"
            ),
            "STORAGE_ENDPOINT": "http://storage:8333",
            "STORAGE_BUCKET": infra.get("STORAGE_BUCKET", "aco-artifacts"),
            "SYNTHETIC_EMAIL_ENABLED": str(self.email == "mock").lower(),
            "SYNTHETIC_EMAIL_INGRESS_ENABLED": str(self.email == "mock").lower(),
            "SYNTHETIC_VOICE_ENABLED": str(self.voice == "mock").lower(),
            "OUTBOUND_ENABLED": self.values.get("OUTBOUND_ENABLED", "false"),
            "AUTOMATION_KILL_SWITCH": self.values.get("AUTOMATION_KILL_SWITCH", "true"),
            "RECORDING_ENABLED": "false",
            "TRANSCRIPTION_ENABLED": "false",
        }
        for key in (
            "OIDC_CLIENT_SECRET",
            "SESSION_SECRET",
            "TOKEN_ENCRYPTION_KEY",
            "STORAGE_APP_ACCESS_KEY",
            "STORAGE_APP_SECRET_KEY",
            "SYNTHETIC_EMAIL_INGRESS_WEBHOOK_SECRET",
        ):
            if infra.get(key):
                result[key] = infra[key]
        # A mock-only configuration without synthetic ingress material stays disabled.
        if not result.get("SYNTHETIC_EMAIL_INGRESS_WEBHOOK_SECRET"):
            result["SYNTHETIC_EMAIL_INGRESS_ENABLED"] = "false"
        if self.values.get("ACO_DELIVERY_APPROVED_EMAIL_RECIPIENT"):
            result["ACO_DELIVERY_APPROVED_EMAIL_RECIPIENT"] = self.values[
                "ACO_DELIVERY_APPROVED_EMAIL_RECIPIENT"
            ]
        result.update({k: v for k, v in self.selection().items() if v})
        return result

    def settings(self) -> "Settings":
        from aco.core.settings import load_settings

        return load_settings(self.backend_environment())

    def web_environment(self) -> dict[str, str]:
        return {
            "ACO_API_INTERNAL_ORIGIN": "http://api:8000",
            "NEXT_TELEMETRY_DISABLED": "1",
        }


def load_development(path: Path) -> DevelopmentConfig:
    try:
        # Keep the final component intact so symlink checks cannot be bypassed by resolve().
        path = path.absolute()
        values = private_values(path)
        if set(values) - ALLOWED or values.get("DEV_MODE") not in {"offline", "live"}:
            raise ValueError(ERROR)
        if values.get("APP_ENV", "local") != "local" or values.get("EXTERNAL_PROVIDERS", "fake") != "fake":
            raise ValueError(ERROR)
        if values.get("ACCOUNTING_ADAPTER", "demo") not in {"demo", "fake"}:
            raise ValueError(ERROR)
        mode = values["DEV_MODE"]
        llm, email, voice = (
            values.get(k, default)
            for k, default in (
                ("LLM_PROVIDER", "fake"),
                ("EMAIL_ADAPTER", "mock"),
                ("VOICE_ADAPTER", "mock"),
            )
        )
        if llm not in {"fake", "openai"} or email not in {"mock", "resend"} or voice not in {"mock", "vapi"}:
            raise ValueError(ERROR)
        if mode == "offline" and (llm, email, voice) != ("fake", "mock", "mock"):
            raise ValueError(ERROR)
        if values.get("ACO_DELIVERY_APPROVED_EMAIL_RECIPIENT"):
            from aco.integrations.ports.email_dto import mailbox

            if mode != "live" or email != "resend":
                raise ValueError(ERROR)
            mailbox(values["ACO_DELIVERY_APPROVED_EMAIL_RECIPIENT"])
        required: set[str] = set()
        if llm == "openai":
            required.update(SELECTORS[:5])
        if email == "resend":
            required.add("ACO_EMAIL_LIVE_CONFIG_PATH")
        if voice == "vapi":
            required.add("ACO_VOICE_LIVE_CONFIG_PATH")
        selected = {key for key in SELECTORS if values.get(key)}
        if selected != required:
            raise ValueError(ERROR)
        for key in selected:
            if key.endswith(("_PATH", "_FILE")) and not Path(values[key]).is_absolute():
                raise ValueError(ERROR)
            if key.endswith(("_GRANT_ID", "_ALLOCATION_ID")):
                UUID(values[key])
        infra_path = Path(values["DEV_INFRA_CONFIG"])
        infra = private_values(infra_path)
        allowed_infra = {
            "PRODUCT_PROJECT", "PRODUCT_WEB_PORT", "PRODUCT_API_PORT", "PRODUCT_IDENTITY_PORT",
            "PRODUCT_DATABASE_URL", "PRODUCT_STORAGE_CONFIG", "PRODUCT_REALM_CONFIG",
            "DATABASE_PASSWORD", "STORAGE_BUCKET", "STORAGE_ACCESS_KEY", "STORAGE_SECRET_KEY",
            "STORAGE_APP_ACCESS_KEY", "STORAGE_APP_SECRET_KEY", "STORAGE_SSE_KEK",
            "KEYCLOAK_ADMIN_USER", "KEYCLOAK_ADMIN_PASSWORD", "OIDC_CLIENT_SECRET", "DEMO_PASSWORD",
            "SESSION_SECRET", "TOKEN_ENCRYPTION_KEY", "SYNTHETIC_EMAIL_ENABLED",
            "SYNTHETIC_VOICE_ENABLED", "SYNTHETIC_EMAIL_INGRESS_ENABLED",
            "PRODUCT_SYNTHETIC_OUTBOUND_ENABLED", "PRODUCT_SYNTHETIC_KILL_SWITCH",
            "SYNTHETIC_EMAIL_INGRESS_WEBHOOK_SECRET",
        }
        database = urlsplit(infra["PRODUCT_DATABASE_URL"])
        if (set(infra) - allowed_infra or database.scheme != "postgresql+psycopg"
                or database.hostname != "postgres" or database.port != 5432
                or database.path != "/aco" or database.query or database.fragment):
            raise ValueError(ERROR)
        project = infra["PRODUCT_PROJECT"]
        if not re.fullmatch(r"aco-product-dev-[a-z0-9][a-z0-9-]{0,25}", project):
            raise ValueError(ERROR)
        ports = [
            int(infra[name])
            for name in (
                "PRODUCT_WEB_PORT",
                "PRODUCT_API_PORT",
                "PRODUCT_IDENTITY_PORT",
            )
        ]
        if len(set(ports)) != 3 or any(not 1024 <= port <= 65535 for port in ports):
            raise ValueError(ERROR)
        result = DevelopmentConfig(
            mode=mode,
            llm=llm,
            email=email,
            voice=voice,
            project=project,
            web_port=ports[0],
            api_port=ports[1],
            identity_port=ports[2],
            provider_directory=Path(values["DEV_PROVIDER_DIRECTORY"]),
            campaign_directory=Path(values["DEV_CAMPAIGN_DIRECTORY"]),
            path=path,
            infra_path=infra_path,
            values=MappingProxyType(values),
            infrastructure=MappingProxyType(infra),
        )
        for directory in (result.provider_directory, result.campaign_directory):
            if (not directory.is_absolute() or directory.is_symlink() or not directory.is_dir()
                    or directory.stat().st_uid != os.getuid()
                    or (directory == result.provider_directory and directory.stat().st_mode & 0o077)):
                raise ValueError(ERROR)
        for key in selected:
            if key.endswith("_PATH") and (
                not Path(values[key]).is_relative_to(Path("/run/aco/providers"))
                or ".." in Path(values[key]).parts
            ):
                raise ValueError(ERROR)
        if values.get("ACO_LIVE_CAMPAIGN_FILE", "/var/lib/aco/live/live-delivery-2026-11-19.sqlite3") != (
            "/var/lib/aco/live/live-delivery-2026-11-19.sqlite3"
        ):
            raise ValueError(ERROR)
        for flag in ("OUTBOUND_ENABLED", "AUTOMATION_KILL_SWITCH"):
            if values.get(flag, "false" if flag == "OUTBOUND_ENABLED" else "true") not in {"true", "false"}:
                raise ValueError(ERROR)
        return result
    except Exception:
        raise ValueError(ERROR) from None
