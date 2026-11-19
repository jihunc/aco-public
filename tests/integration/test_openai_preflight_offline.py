"""Exercise real maintained SDK + PydanticAI against a synthetic HTTP double."""

import json
from typing import Any

import httpx2 as httpx
from provider_smoke_support import private_config

from aco.integrations.openai.preflight import run_preflight
from aco.integrations.provider_smoke.config import AUTHORIZATION_REF, load_config
from aco.integrations.provider_smoke.transport import SmokeTransport


async def test_actual_sdk_typed_output_and_read_tool_preserve_evidence(tmp_path: Any) -> None:
    cfg = load_config(private_config(tmp_path), authorization_ref=AUTHORIZATION_REF)
    seen = []

    async def reply(request: Any) -> Any:
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(
                200, json={"id": cfg.model, "object": "model", "created": 1, "owned_by": "openai"}
            )
        body = json.loads(request.content)
        assert body["store"] is False
        assert body["model"] == cfg.model
        if len(seen) == 2:
            output: list[dict[str, Any]] = [
                {
                    "type": "function_call",
                    "id": "fc_synthetic",
                    "call_id": "call_synthetic",
                    "name": "get_synthetic_invoice",
                    "arguments": "{}",
                    "status": "completed",
                }
            ]
        else:
            assert any(x.get("type") == "function_call_output" for x in body["input"])
            output = [
                {
                    "type": "message",
                    "id": "msg_synthetic",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": '{"balance":12000}', "annotations": []}],
                }
            ]
        return httpx.Response(
            200,
            json={
                "id": f"resp_synthetic_{len(seen)}",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "error": None,
                "incomplete_details": None,
                "model": cfg.model,
                "output": output,
                "parallel_tool_calls": False,
                "tools": [],
                "tool_choice": "auto",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )

    gate = SmokeTransport(cfg, "openai", inner=httpx.MockTransport(reply))
    result = await run_preflight(cfg, gate)
    assert result["status"] == "PASS"
    assert result["input_tokens"] == 200
    assert result["output_tokens"] == 40
    assert result["provider_response_ids"] == ["resp_synthetic_2", "resp_synthetic_3"]
    assert result["safe_tool_results"] == [{"balance": 12000}]
    assert result["cost_quality"] == "UNKNOWN"
    assert result["cost_amount"] is None
    assert gate.requests == 3
    assert "fake-secret" not in json.dumps(result)


async def test_sdk_rate_limit_never_retries_and_does_not_leak_body(tmp_path: Any) -> None:
    import pytest
    from openai import RateLimitError

    cfg = load_config(private_config(tmp_path), authorization_ref=AUTHORIZATION_REF)
    seen = []

    async def reply(request: Any) -> Any:
        seen.append(request.method)
        return httpx.Response(429, json={"error": {"message": "private provider body", "type": "rate_limit"}})

    gate = SmokeTransport(cfg, "openai", inner=httpx.MockTransport(reply))
    with pytest.raises(RateLimitError):
        await run_preflight(cfg, gate)
    assert seen == ["GET"]
    assert gate.statuses == [429]


async def test_unknown_price_only_retrieves_model_metadata(tmp_path: Any) -> None:
    import pytest

    from aco.integrations.provider_smoke.config import SmokeBlocked

    path = private_config(tmp_path)
    path.write_text(path.read_text().replace("gpt-6.1-sol", "unpriced-model"))
    cfg = load_config(path, authorization_ref=AUTHORIZATION_REF)
    seen = []

    async def reply(request: Any) -> Any:
        seen.append(request.method)
        return httpx.Response(
            200, json={"id": "unpriced-model", "object": "model", "created": 1, "owned_by": "openai"}
        )

    gate = SmokeTransport(cfg, "openai", inner=httpx.MockTransport(reply))
    with pytest.raises(SmokeBlocked, match="VERIFIED_CURRENT_PRICE_REQUIRED"):
        await run_preflight(cfg, gate)
    assert seen == ["GET"]
