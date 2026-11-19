"""Explicit server binding, never ambient credentials or live activation."""

from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr

from aco.core.types import StrictModel


class VapiBinding(StrictModel):
    tenant_id: UUID
    connection_id: UUID
    assistant_id: UUID
    api_key: SecretStr
    webhook_secret: SecretStr
    credential_revision: str = Field(min_length=1, max_length=200)
    server_credential_id: UUID | None = None
    evidence_mode: Literal["LIVE", "MOCKED_HTTP"] = "LIVE"
    max_duration_seconds: int = Field(default=300, ge=1, le=300)
    control_api_key: SecretStr | None = None
    control_hosts: tuple[str, ...] = ()
