"""Narrow explicit command: no application settings, arbitrary URLs, sends, or calls."""

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO
from uuid import uuid4

import httpx2

from aco.integrations.provider_smoke.config import AUTHORIZATION_REF, SmokeBlocked, SmokeConfig, load_config
from aco.integrations.provider_smoke.transport import SmokeTransport


def safe_id(value: object) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value) else None


async def execute(config: SmokeConfig, operation: str, transport: SmokeTransport) -> dict[str, Any]:
    if operation == "openai":
        from aco.integrations.openai.preflight import run_preflight

        return await run_preflight(config, transport)
    if operation == "resend":
        key, url = config.resend_api_key, "https://api.resend.com/domains"
    elif operation == "vapi":
        key = config.vapi_api_key
        url = "https://api.vapi.ai/assistant" + (f"/{config.assistant_id}" if config.assistant_id else "")
    else:
        raise SmokeBlocked("UNKNOWN_OPERATION")
    if not key.get_secret_value():
        raise SmokeBlocked("PROVIDER_KEY_REQUIRED")
    async with httpx2.AsyncClient(
        transport=transport, trust_env=False, follow_redirects=False, timeout=20
    ) as http:
        response = await http.get(url, headers={"Authorization": f"Bearer {key.get_secret_value()}"})
        response.raise_for_status()
        data = response.json()
    if operation == "resend":
        rows = data["data"]
    elif config.assistant_id:
        if data.get("id") != config.assistant_id:
            raise SmokeBlocked("ASSISTANT_ID_MISMATCH")
        rows = [data]
    else:
        rows = data
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or not safe_id(row.get("id")) for row in rows
    ):
        raise SmokeBlocked("INVALID_PROVIDER_IDENTITY")
    return {
        "status": "PASS",
        "scope": "read_only_connectivity_only",
        "count": len(rows),
        "native_ids": [row["id"] for row in rows],
        "cost_quality": "UNKNOWN",
        "cost_amount": None,
        "currency": "USD",
    }


def append_evidence(handle: TextIO, value: dict[str, Any]) -> None:
    handle.write(json.dumps(value, sort_keys=True) + "\n")
    handle.flush()
    os.fsync(handle.fileno())


def failure_result(error: Exception) -> dict[str, Any]:
    # Never stringify vendor errors: they can contain request bodies/keys.
    cause: BaseException | None = error
    blocked = None
    while cause is not None:
        if isinstance(cause, SmokeBlocked):
            blocked = str(cause)
            break
        cause = cause.__cause__
    proof_failures = {
        "TYPED_TOOL_PROOF_FAILED",
        "MODEL_ID_MISMATCH",
        "RESPONSE_MODEL_MISMATCH",
        "OBSERVED_USAGE_REQUIRED",
        "ASSISTANT_ID_MISMATCH",
        "INVALID_PROVIDER_IDENTITY",
    }
    return {
        "status": "BLOCKED" if blocked and blocked not in proof_failures else "FAIL",
        "code": blocked or "PROVIDER_PREFLIGHT_FAILED",
        "cost_quality": "UNKNOWN",
        "cost_amount": None,
        "currency": "USD",
    }


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("operation", choices=("openai", "resend", "vapi"))
    parser.add_argument("--authorization-ref", required=True, choices=(AUTHORIZATION_REF,))
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    # Nothing from ambient SDK/proxy/tracing environment can select a route or log
    # secrets. Isolated Python startup in scripts/provider-smoke runs before this.
    os.environ.clear()
    logging.disable(logging.CRITICAL)
    try:
        config = load_config(args.config, authorization_ref=args.authorization_ref)
        # This explicit authorization is for this bounded milestone, not perpetual.
        if not datetime(2026, 11, 18, tzinfo=UTC) <= datetime.now(UTC) < datetime(2026, 11, 20, tzinfo=UTC):
            raise SmokeBlocked("AUTHORIZATION_WINDOW_EXPIRED")
        descriptor = os.open(args.evidence, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except (SmokeBlocked, OSError) as error:
        code = str(error) if isinstance(error, SmokeBlocked) else "EVIDENCE_FILE_UNAVAILABLE"
        print(json.dumps({"status": "BLOCKED", "code": code}))
        return 2
    transport = SmokeTransport(config, args.operation)
    # Fixed read-only Git command; no config/provider input becomes command text.
    commit = subprocess.run(
        ["/usr/bin/git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    base = {
        "attempt_id": str(uuid4()),
        "authorization_ref": AUTHORIZATION_REF,
        "operation": args.operation,
        "git_commit": commit,
        "started_at": datetime.now(UTC).isoformat(),
        "lane": "LIVE",
        "max_requests": config.max_requests,
        "max_input_tokens": config.max_input_tokens,
        "max_output_tokens": config.max_output_tokens,
        "max_usd": str(config.max_usd),
    }
    with os.fdopen(descriptor, "w") as evidence:
        append_evidence(evidence, {**base, "status": "PENDING", "cost_quality": "UNKNOWN"})
        try:
            result = asyncio.run(execute(config, args.operation, transport))
        except Exception as error:
            result = failure_result(error)
        final = {
            **base,
            **result,
            "finished_at": datetime.now(UTC).isoformat(),
            "http_requests": transport.requests,
            "http_statuses": transport.statuses,
            "request_ids": transport.request_ids,
            "response_usage": transport.response_usage,
            "reserved_input_tokens": transport.input_bound,
            "reserved_output_tokens": transport.output_bound,
        }
        append_evidence(evidence, final)
    print(json.dumps(final, sort_keys=True))
    return 0 if final["status"] == "PASS" else 2
