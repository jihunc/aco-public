"""The smoke boundary must refuse unsafe configuration and wire requests before IO."""

from datetime import UTC, datetime
from typing import Any

import httpx2 as httpx
import pytest
from provider_smoke_support import private_config

from aco.integrations.provider_smoke import config, transport


def test_private_config_requires_authorization_and_does_not_read_ambient_env(
    tmp_path: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-secret")
    path = private_config(tmp_path)
    with pytest.raises(config.SmokeBlocked):
        config.load_config(path, authorization_ref="unknown")
    loaded = config.load_config(path, authorization_ref=config.AUTHORIZATION_REF)
    assert loaded.openai_api_key.get_secret_value() == "fake-secret"
    assert "fake-secret" not in repr(loaded)
    assert loaded.max_requests == 3
    path.chmod(0o644)
    with pytest.raises(config.SmokeBlocked):
        config.load_config(path, authorization_ref=config.AUTHORIZATION_REF)


@pytest.mark.parametrize(
    "extra",
    [
        "OPENAI_API_KEY=duplicate\n",
        "UNEXPECTED_BASE_URL=https://evil.example\n",
        "ACO_LIVE_MAX_USD=NaN\n",
        "ACO_LIVE_MAX_REQUESTS=4\n",
        "ACO_LIVE_KILL_SWITCH=true\n",
        "LLM_MODEL=$(command)\n",
    ],
)
def test_config_fails_closed_on_unsafe_values(tmp_path: Any, extra: Any) -> None:
    with pytest.raises(config.SmokeBlocked):
        config.load_config(private_config(tmp_path, extra), authorization_ref=config.AUTHORIZATION_REF)


async def test_transport_blocks_extra_hosts_redirects_and_request_overrun(tmp_path: Any) -> None:
    cfg = config.load_config(private_config(tmp_path), authorization_ref=config.AUTHORIZATION_REF)
    seen = []

    async def reply(request: Any) -> Any:
        seen.append(str(request.url))
        return httpx.Response(200, json={"id": "gpt-6.1-sol"})

    gate = transport.SmokeTransport(cfg, "openai", inner=httpx.MockTransport(reply))
    async with httpx.AsyncClient(transport=gate, trust_env=False, follow_redirects=False) as client:
        for url in [
            "http://api.openai.com/v1/models/gpt-6.1-sol",
            "https://evil.example/v1/models/gpt-6.1-sol",
            "https://api.openai.com/v1/models/gpt-6.1-sol?extra=1",
        ]:
            with pytest.raises(config.SmokeBlocked):
                await client.get(url)
        await client.get("https://api.openai.com/v1/models/gpt-6.1-sol")
        with pytest.raises(config.SmokeBlocked):
            await client.get("https://api.openai.com/v1/models/gpt-6.1-sol")
    assert seen == ["https://api.openai.com/v1/models/gpt-6.1-sol"]


def test_unknown_model_or_stale_price_blocks_paid_preflight(tmp_path: Any) -> None:
    cfg = config.load_config(private_config(tmp_path), authorization_ref=config.AUTHORIZATION_REF)
    assert transport.price_bound(cfg, now=datetime(2026, 11, 18, tzinfo=UTC)) < cfg.max_usd
    with pytest.raises(config.SmokeBlocked):
        transport.price_bound(cfg, now=datetime(2027, 1, 1, tzinfo=UTC))
    with pytest.raises(config.SmokeBlocked):
        transport.price_bound(
            cfg.model_copy(update={"model": "unknown-model"}), now=datetime(2026, 11, 18, tzinfo=UTC)
        )


@pytest.mark.parametrize(
    "operation,body",
    [
        ("resend", {"data": [{"id": "domain_synthetic", "name": "private.example", "status": "verified"}]}),
        ("vapi", [{"id": "assistant_synthetic", "model": {"secret": "private configuration"}}]),
    ],
)
async def test_readonly_provider_smoke_retains_ids_not_private_content(
    tmp_path: Any, operation: Any, body: Any
) -> None:
    from aco.integrations.provider_smoke.runner import execute

    path = private_config(tmp_path, "RESEND_API_KEY=fake-resend\nVAPI_API_KEY=fake-vapi\n")
    cfg = config.load_config(path, authorization_ref=config.AUTHORIZATION_REF)
    attempts = []

    async def reply(request: Any) -> Any:
        attempts.append((request.method, request.url.host, request.url.path))
        return httpx.Response(200, json=body)

    gate = transport.SmokeTransport(cfg, operation, inner=httpx.MockTransport(reply))
    result = await execute(cfg, operation, gate)
    assert result["status"] == "PASS"
    assert result["native_ids"] == (
        ["domain_synthetic"] if operation == "resend" else ["assistant_synthetic"]
    )
    assert "private" not in str(result)
    assert len(attempts) == 1
    assert attempts[0][0] == "GET"


async def test_redirect_and_revoked_private_switch_never_reach_second_endpoint(tmp_path: Any) -> None:
    path = private_config(tmp_path)
    cfg = config.load_config(path, authorization_ref=config.AUTHORIZATION_REF)
    attempts = []

    async def redirect(request: Any) -> Any:
        attempts.append(request)
        return httpx.Response(302, headers={"location": "https://evil.example/steal"})

    gate = transport.SmokeTransport(cfg, "openai", inner=httpx.MockTransport(redirect))
    async with httpx.AsyncClient(transport=gate, follow_redirects=True, trust_env=False) as client:
        with pytest.raises(config.SmokeBlocked, match="REDIRECT_REFUSED"):
            await client.get("https://api.openai.com/v1/models/gpt-6.1-sol")
    assert len(attempts) == 1
    path.write_text(path.read_text().replace("ACO_LIVE_KILL_SWITCH=false", "ACO_LIVE_KILL_SWITCH=true"))
    gate = transport.SmokeTransport(cfg, "openai", inner=httpx.MockTransport(redirect))
    async with httpx.AsyncClient(transport=gate, trust_env=False) as client:
        with pytest.raises(config.SmokeBlocked, match="KILL_SWITCH"):
            await client.get("https://api.openai.com/v1/models/gpt-6.1-sol")
    assert len(attempts) == 1


def test_price_policy_block_remains_blocked_after_readonly_metadata() -> None:
    from aco.integrations.provider_smoke.runner import failure_result

    failure = failure_result(config.SmokeBlocked("VERIFIED_CURRENT_PRICE_REQUIRED"))
    assert failure["status"] == "BLOCKED"
    assert failure["code"] == "VERIFIED_CURRENT_PRICE_REQUIRED"
    assert failure_result(RuntimeError("private provider body")) == {
        "status": "FAIL",
        "code": "PROVIDER_PREFLIGHT_FAILED",
        "cost_quality": "UNKNOWN",
        "cost_amount": None,
        "currency": "USD",
    }
