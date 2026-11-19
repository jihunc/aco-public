"""Real maintained SDK batch reads remain serial and bounded; synthetic HTTP only."""

import asyncio
import json
from typing import Any

import httpx2 as httpx
import pytest
from llm_support import Balance, Lookup, setup_adapter
from openai_support import RecordingGuard, response
from pydantic import SecretStr

from aco.core.types import ProviderErrorCode
from aco.integrations.openai.adapter import OpenAIModelAdapter
from aco.integrations.openai.settings import OpenAISettings
from aco.integrations.ports.llm import LLMExecutionBlocked, LLMRequestPolicy, ReadTool


@pytest.mark.parametrize("boundary", ["two_invoices", "revoked", "tool_cap"])
async def test_batched_read_tools_keep_independent_serial_authority(boundary: str) -> None:
    requests: list[dict[str, Any]] = []
    invoked: list[str] = []
    active, maximum = 0, 0
    count = 9 if boundary == "tool_cap" else 4

    async def reply(request: Any) -> Any:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return response(
                response_id="resp_batch",
                output=[
                    {
                        "type": "function_call",
                        "id": f"fc_batch_{i}",
                        "call_id": f"batch_{i}",
                        "name": "get_invoice" if i % 2 == 0 else "get_verified_payments",
                        "arguments": json.dumps({"invoice": f"INV-{i // 2 + 1}"}),
                        "status": "completed",
                    }
                    for i in range(count)
                ],
            )
        assert len([v for v in body["input"] if v.get("type") == "function_call_output"]) == 4
        return response(response_id="resp_complete")

    async def read(args: Any) -> Balance:
        nonlocal active, maximum
        if boundary == "revoked" and invoked:
            raise LLMExecutionBlocked(ProviderErrorCode.AUTH_EXPIRED)
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(0)
            invoked.append(args.invoice)
            return Balance(amount=12000)
        finally:
            active -= 1

    _, context = setup_adapter()
    guard = RecordingGuard()
    adapter = OpenAIModelAdapter(
        tenant_id=context.tenant_id,
        connection_id=context.connection_id,
        settings=OpenAISettings(
            api_key=SecretStr("offline-placeholder"),
            model_id="gpt-6.1-sol",
            model_route="investigation-v1",
            features=frozenset({"structured_output", "tool_calling"}),
        ),
        execution_guard=guard,
        transport_factory=lambda: httpx.MockTransport(reply),
    )
    result = await adapter.run_typed(
        "investigation-v1",
        context,
        tuple(ReadTool(name, Lookup, Balance, read) for name in ("get_invoice", "get_verified_payments")),
        Balance,
        LLMRequestPolicy(max_requests=2, max_tool_calls=8, max_input_tokens=32000, max_output_tokens=2048),
    )
    assert requests[0]["parallel_tool_calls"] is True
    assert maximum == (0 if boundary == "tool_cap" else 1)
    if boundary == "two_invoices":
        assert result.error is None and result.output == Balance(amount=12000)
        assert invoked == ["INV-1", "INV-1", "INV-2", "INV-2"]
        assert len(result.tools) == 4
        assert len(result.attempts) == len(guard.before) == len(guard.after) == len(requests) == 2
        assert all(t.request_attempt_id == result.attempts[0].attempt_id for t in result.tools)
    else:
        assert result.output is None and result.error is not None
        assert len(result.attempts) == len(requests) == len(guard.before) == len(guard.after) == 1
        if boundary == "revoked":
            assert invoked == ["INV-1"] and result.error.code == ProviderErrorCode.AUTH_EXPIRED
        else:
            assert len(invoked) <= 8
            assert len(result.tools) <= 8
