"""Explicit operational smoke authorization; never application activation."""

import os
import re
import stat
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, SecretStr

from aco.core.live import DELIVERY_LIVE_AUTHORIZATION, LEGACY_LIVE_AUTHORIZATION

AUTHORIZATION_REF = LEGACY_LIVE_AUTHORIZATION
ALLOWED = frozenset(
    {
        "OPENAI_API_KEY",
        "LLM_MODEL",
        "RESEND_API_KEY",
        "RESEND_WEBHOOK_SECRET",
        "RESEND_SENDER_ADDRESS",
        "RESEND_INBOUND_ADDRESS",
        "VAPI_API_KEY",
        "VAPI_ASSISTANT_ID",
        "VAPI_WEBHOOK_SECRET",
        "ACO_LIVE_MAX_REQUESTS",
        "ACO_LIVE_MAX_INPUT_TOKENS",
        "ACO_LIVE_MAX_OUTPUT_TOKENS",
        "ACO_LIVE_MAX_USD",
        "ACO_LIVE_KILL_SWITCH",
    }
)


class SmokeBlocked(RuntimeError):
    """Messages are fixed safe codes, never provider/config payloads."""


class SmokeConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    openai_api_key: SecretStr
    resend_api_key: SecretStr
    vapi_api_key: SecretStr
    model: str
    assistant_id: str
    max_requests: int
    max_input_tokens: int
    max_output_tokens: int
    max_usd: Decimal
    source_path: Path
    authorization_ref: str = AUTHORIZATION_REF


def load_config(
    path: Path,
    *,
    authorization_ref: str,
    lane: Literal["preflight", "investigation"] = "preflight",
) -> SmokeConfig:
    if authorization_ref != AUTHORIZATION_REF and not (
        lane == "investigation" and authorization_ref == DELIVERY_LIVE_AUTHORIZATION
    ):
        raise SmokeBlocked("AUTHORIZATION_REQUIRED")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as handle:
            meta = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(meta.st_mode)
                or stat.S_IMODE(meta.st_mode) != 0o600
                or meta.st_uid != os.getuid()
            ):
                raise SmokeBlocked("PRIVATE_CONFIG_REQUIRED")
            content = handle.read(16385)
        if len(content) > 16384:
            raise SmokeBlocked("CONFIG_TOO_LARGE")
        values: dict[str, str] = {}
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not separator or key not in ALLOWED or key in values or "$" in value or "`" in value:
                raise SmokeBlocked("INVALID_CONFIG")
            if value[:1] in {"'", '"'}:
                if value[-1:] != value[:1]:
                    raise SmokeBlocked("INVALID_CONFIG")
                value = value[1:-1]
            if any(ord(c) < 32 for c in value):
                raise SmokeBlocked("INVALID_CONFIG")
            values[key] = value
        if values.get("ACO_LIVE_KILL_SWITCH", "true") != "false":
            raise SmokeBlocked("SMOKE_KILL_SWITCH_ENABLED")
        caps = [
            int(values.get(key, "0"))
            for key in ("ACO_LIVE_MAX_REQUESTS", "ACO_LIVE_MAX_INPUT_TOKENS", "ACO_LIVE_MAX_OUTPUT_TOKENS")
        ]
        if lane == "investigation":
            # Current delivery authorization supersedes the consumed one-run limit;
            # this remains an explicit private cap, never automatic spending authority.
            input_ceiling = 64000 if authorization_ref == DELIVERY_LIVE_AUTHORIZATION else 32000
            ceilings = (3, input_ceiling, 2048)
        else:
            ceilings = (3, 2048, 512)
        if any(value <= 0 or value > ceiling for value, ceiling in zip(caps, ceilings, strict=True)):
            raise SmokeBlocked("INVALID_LIMITS")
        amount = Decimal(values.get("ACO_LIVE_MAX_USD", "0"))
        if not amount.is_finite() or not Decimal("0") < amount <= Decimal("1"):
            raise SmokeBlocked("INVALID_LIMITS")
        model = values.get("LLM_MODEL", "")
        if model and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}", model):
            raise SmokeBlocked("INVALID_MODEL")
        assistant = values.get("VAPI_ASSISTANT_ID", "")
        if assistant and not re.fullmatch(r"[0-9a-fA-F-]{36}", assistant):
            raise SmokeBlocked("INVALID_ASSISTANT")
        return SmokeConfig(
            openai_api_key=SecretStr(values.get("OPENAI_API_KEY", "")),
            resend_api_key=SecretStr(values.get("RESEND_API_KEY", "")),
            vapi_api_key=SecretStr(values.get("VAPI_API_KEY", "")),
            model=model,
            assistant_id=assistant,
            max_requests=caps[0],
            max_input_tokens=caps[1],
            max_output_tokens=caps[2],
            max_usd=amount,
            source_path=path,
            authorization_ref=authorization_ref,
        )
    except SmokeBlocked:
        raise
    except (OSError, ValueError, ArithmeticError):
        raise SmokeBlocked("INVALID_PRIVATE_CONFIG") from None
