"""Server-only explicit connection bindings; never loaded from browser fields or SDK globals."""

from dataclasses import dataclass
from ipaddress import ip_address
from typing import Literal
from uuid import UUID

from pydantic import SecretStr

from aco.core.types import CostQuality, ProviderCost
from aco.integrations.ports.email_dto import mailbox


@dataclass(frozen=True)
class ResendBinding:
    tenant_id: UUID
    connection_id: UUID
    api_key: SecretStr
    inbox_addresses: tuple[str, ...]
    download_hosts: frozenset[str]
    sender_addresses: tuple[str, ...] = ()
    webhook_secret: SecretStr | None = None
    billing_currency: str = "USD"
    evidence_mode: Literal["LIVE", "MOCKED_HTTP"] = "LIVE"
    max_source_bytes: int = 40 * 1024 * 1024

    def __post_init__(self) -> None:
        ProviderCost(amount=None, currency=self.billing_currency, quality=CostQuality.UNKNOWN)
        if self.evidence_mode not in {"LIVE", "MOCKED_HTTP"}:
            raise ValueError("Explicit provider evidence mode is required")
        if any(c in self.api_key.get_secret_value() for c in "\r\n"):
            raise ValueError("Credential header contains invalid bytes")
        if (
            not self.api_key.get_secret_value()
            or len(self.inbox_addresses) > 20
            or len(self.sender_addresses) > 20
            or not (self.inbox_addresses or self.sender_addresses)
        ):
            raise ValueError(
                "A server-resolved credential and explicit receiving or sending scope are required"
            )
        for address in (*self.inbox_addresses, *self.sender_addresses):
            mailbox(address)
        for host in self.download_hosts:
            if (
                host != host.lower()
                or "." not in host
                or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-." for c in host)
            ):
                raise ValueError("Exact provider download DNS hosts are required")
            try:
                ip_address(host)
            except ValueError:
                pass
            else:
                raise ValueError("Download IP literals are unsupported")
            if host.endswith((".localhost", ".local", ".internal")):
                raise ValueError("Local download destinations are forbidden")
        if not 1 <= self.max_source_bytes <= 40 * 1024 * 1024:
            raise ValueError("Source capture must have an explicit bounded size")
