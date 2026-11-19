"""Actual OpenAI/PydanticAI stack, synthetic HTTP only; no live acceptance claim."""

import json
from typing import Any
from uuid import uuid4

import httpx2 as httpx
import pytest
from llm_support import Balance, Lookup, setup_adapter
from openai_support import RecordingGuard, response
from pydantic import SecretStr

from aco.core.types import ProviderErrorCode
from aco.integrations.openai.adapter import OpenAIModelAdapter
from aco.integrations.openai.settings import OpenAISettings
from aco.integrations.ports.llm import LLMExecutionBlocked, LLMRequestPolicy, ReadTool


def build(
    reply: Any, *, guard: Any = None, features: Any = frozenset({"structured_output", "tool_calling"})
) -> Any:
    _, context = setup_adapter()
    guard = guard or RecordingGuard()
    adapter = OpenAIModelAdapter(
        tenant_id=context.tenant_id,
        connection_id=context.connection_id,
        settings=OpenAISettings(
            api_key=SecretStr("test-placeholder"),
            model_id="gpt-6.1-sol",
            model_route="investigation-v1",
            features=features,
        ),
        execution_guard=guard,
        transport_factory=lambda: httpx.MockTransport(reply),
    )
    return adapter, context, guard


async def test_capability_input_ceiling_matches_validated_policy_without_provider_io() -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        pytest.fail("Capability discovery must not make a provider request")

    adapter, _, guard = build(forbidden)
    capabilities = await adapter.describe_capabilities()
    assert capabilities.limits["max_input_tokens"] == 64000
    assert (
        LLMRequestPolicy(max_input_tokens=capabilities.limits["max_input_tokens"]).max_input_tokens == 64000
    )
    assert LLMRequestPolicy().max_input_tokens == 4096
    assert guard.before == [] and guard.after == []


async def test_actual_sdk_safe_read_and_typed_output_preserve_each_attempt() -> None:
    requests, reads = [], []

    async def reply(request: Any) -> Any:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return response(
                response_id="resp_first",
                output=[
                    {
                        "type": "function_call",
                        "id": "fc_test",
                        "call_id": "call_test",
                        "name": "get_invoice",
                        "arguments": '{"invoice":"INV-1"}',
                        "status": "completed",
                    }
                ],
            )
        assert any(i.get("type") == "function_call_output" for i in requests[-1]["input"])
        return response(response_id="resp_second")

    async def read(args: Any) -> Any:
        reads.append(args.invoice)
        return Balance(amount=12000)

    adapter, context, guard = build(reply)
    result = await adapter.run_typed(
        "investigation-v1",
        context,
        (ReadTool("get_invoice", Lookup, Balance, read),),
        Balance,
        LLMRequestPolicy(max_input_tokens=16000),
    )
    assert result.output == Balance(amount=12000)
    assert reads == ["INV-1"]
    assert [a.provider_reference.external_id for a in result.attempts] == ["resp_first", "resp_second"]
    assert len({a.attempt_id for a in result.attempts}) == 2
    assert result.usage.input_tokens == 220 and result.usage.output_tokens == 42
    assert result.usage.cached_input_tokens == 14
    assert len(guard.before) == len(guard.after) == 2
    assert all(i.run_attempt_id == result.attempt_id for i in guard.before)
    assert result.tools[0].output == Balance(amount=12000)
    assert result.tools[0].call_id == "call_test"
    assert result.tools[0].request_attempt_id == result.attempts[0].attempt_id
    assert result.cost.amount is None and result.cost.quality.value == "UNKNOWN"
    assert all(r["store"] is False and r["service_tier"] == "default" for r in requests)
    assert result.provider_reference.external_id == "resp_second"
    assert "test-placeholder" not in result.model_dump_json()


@pytest.mark.parametrize(
    "denial", ["tenant", "connection", "route", "capability", "unsafe_tool", "absent_guard", "guard"]
)
async def test_authority_capability_and_guard_denials_have_zero_wire_or_tool_calls(denial: Any) -> None:
    requests, reads = [], []

    async def reply(request: Any) -> Any:
        requests.append(request)
        return response()

    async def read(args: Any) -> Any:
        reads.append(args)
        return Balance(amount=12000)

    adapter, context, guard = build(
        reply,
        guard=RecordingGuard(deny_on=1),
        features=frozenset() if denial == "capability" else frozenset({"structured_output", "tool_calling"}),
    )
    if denial in {"tenant", "connection"}:
        context = context.model_copy(update={f"{denial}_id": uuid4()})
    if denial == "absent_guard":
        adapter.execution_guard = None
    result = await adapter.run_typed(
        "bad-route" if denial == "route" else "investigation-v1",
        context,
        (ReadTool("send_email" if denial == "unsafe_tool" else "get_invoice", Lookup, Balance, read),),
        Balance,
        LLMRequestPolicy(),
    )
    assert result.output is None and result.error is not None
    assert not result.provider_attempted and not result.attempts
    assert not requests and not reads
    assert len(guard.before) == (1 if denial == "guard" else 0)


@pytest.mark.parametrize(
    "status,code",
    [(401, "AUTH_EXPIRED"), (429, "RATE_LIMITED"), (400, "VALIDATION_FAILED"), (500, "UNKNOWN_SIDE_EFFECT")],
)
async def test_http_failures_no_retry_and_redacted_observed_attempt(status: Any, code: Any) -> None:
    requests = []

    async def reply(request: Any) -> Any:
        requests.append(request)
        return httpx.Response(
            status,
            json={"error": {"message": "private provider secret", "type": "error"}},
            headers={"x-request-id": "req_failed"},
        )

    adapter, context, guard = build(reply)
    result = await adapter.run_typed("investigation-v1", context, (), Balance, LLMRequestPolicy())
    assert result.error.code.value == code
    assert result.usage.requests == len(requests) == len(result.attempts) == 1
    assert result.attempts[0].request_id == "req_failed"
    assert len(guard.after) == 1
    assert "private" not in result.model_dump_json()


async def test_kill_between_turns_retains_observed_usage_and_tool_evidence() -> None:
    async def reply(request: Any) -> Any:
        return response(
            output=[
                {
                    "type": "function_call",
                    "id": "fc_test",
                    "call_id": "call_test",
                    "name": "get_invoice",
                    "arguments": '{"invoice":"INV-1"}',
                    "status": "completed",
                }
            ]
        )

    async def read(args: Any) -> Any:
        return Balance(amount=12000)

    adapter, context, guard = build(reply, guard=RecordingGuard(deny_on=2))
    result = await adapter.run_typed(
        "investigation-v1",
        context,
        (ReadTool("get_invoice", Lookup, Balance, read),),
        Balance,
        LLMRequestPolicy(max_input_tokens=16000),
    )
    assert result.output is None and result.error.code.value == "VALIDATION_FAILED"
    assert result.usage.requests == 1 and result.usage.input_tokens == 110
    assert len(result.tools) == 1 and len(guard.after) == 1


async def test_after_request_failure_preserves_response_without_replay() -> None:
    adapter, context, guard = build(lambda request: response(), guard=RecordingGuard(fail_after=True))
    result = await adapter.run_typed("investigation-v1", context, (), Balance, LLMRequestPolicy())
    assert result.output is None and result.reconciliation_required
    assert result.provider_reference.external_id == "resp_test"
    assert result.usage.input_tokens == 110 and len(result.attempts) == 1
    assert "private" not in result.model_dump_json()


@pytest.mark.parametrize("kind", ["invalid", "refusal", "wrong_model", "missing_usage", "timeout"])
async def test_provider_failure_preserves_partial_provenance(kind: Any) -> None:
    async def reply(request: Any) -> Any:
        if kind == "timeout":
            raise httpx.ReadTimeout("private timeout")
        if kind == "wrong_model":
            return response(model="unexpected-model")
        if kind == "missing_usage":
            return response(usage=False)
        content = (
            {"type": "refusal", "refusal": "private refusal"}
            if kind == "refusal"
            else {"type": "output_text", "text": '{"amount":"bad"}', "annotations": []}
        )
        return response(
            output=[
                {
                    "type": "message",
                    "id": "msg_test",
                    "role": "assistant",
                    "status": "completed",
                    "content": [content],
                }
            ]
        )

    adapter, context, guard = build(reply)
    result = await adapter.run_typed("investigation-v1", context, (), Balance, LLMRequestPolicy())
    assert result.output is None and result.error is not None
    assert result.provider_attempted and len(result.attempts) == len(guard.after) == 1
    assert "private" not in result.model_dump_json()
    if kind == "timeout":
        assert result.error.code.value == "UNKNOWN_SIDE_EFFECT" and result.error.may_have_side_effect
    if kind == "wrong_model":
        assert result.attempts[0].model_id == "unexpected-model"
    if kind == "missing_usage":
        assert result.usage.input_tokens is None


async def test_uuid_tool_input_and_failed_authorized_read_keep_evidence() -> None:
    from uuid import UUID

    from aco.core.types import StrictModel

    class InvoiceID(StrictModel):
        invoice_id: UUID

    invoice_id = uuid4()
    reads = []

    async def reply(request: Any) -> Any:
        return response(
            output=[
                {
                    "type": "function_call",
                    "id": "fc_test",
                    "call_id": "call_test",
                    "name": "get_invoice",
                    "arguments": json.dumps({"invoice_id": str(invoice_id)}),
                    "status": "completed",
                }
            ]
        )

    async def denied(args: Any) -> Any:
        reads.append(args.invoice_id)
        raise LLMExecutionBlocked(ProviderErrorCode.AUTH_EXPIRED)

    adapter, context, guard = build(reply)
    result = await adapter.run_typed(
        "investigation-v1",
        context,
        (ReadTool("get_invoice", InvoiceID, Balance, denied),),
        Balance,
        LLMRequestPolicy(max_input_tokens=16000),
    )
    assert reads == [invoice_id]
    assert result.error.code.value == "AUTH_EXPIRED"
    assert len(result.tools) == 1 and result.tools[0].output is None
    assert result.tools[0].error.code.value == "AUTH_EXPIRED"
    assert result.usage.requests == 1


@pytest.mark.parametrize("boundary", ["input", "requests", "tool_calls", "guard_failure", "redirect"])
async def test_hard_bounds_stop_without_extra_requests(boundary: Any) -> None:
    requests = []

    async def reply(request: Any) -> Any:
        requests.append(request)
        if boundary == "redirect":
            return httpx.Response(307, headers={"location": "https://forbidden.invalid/private"})
        return response(
            output=[
                {
                    "type": "function_call",
                    "id": "fc_test",
                    "call_id": "call_test",
                    "name": "get_invoice",
                    "arguments": '{"invoice":"INV-1"}',
                    "status": "completed",
                }
            ]
        )

    async def read(args: Any) -> Any:
        return Balance(amount=12000)

    class FailingGuard(RecordingGuard):
        async def before_request(self, intent: Any) -> Any:
            raise RuntimeError("private guard failure")

    adapter, context, guard = build(reply, guard=FailingGuard() if boundary == "guard_failure" else None)
    policy = LLMRequestPolicy(
        max_input_tokens=1 if boundary == "input" else 32000,
        max_requests=1 if boundary == "requests" else 3,
        max_tool_calls=1 if boundary == "tool_calls" else 4,
    )
    result = await adapter.run_typed(
        "investigation-v1", context, (ReadTool("get_invoice", Lookup, Balance, read),), Balance, policy
    )
    assert result.output is None and result.error is not None
    assert (
        len(requests)
        == {"input": 0, "guard_failure": 0, "requests": 1, "tool_calls": 2, "redirect": 1}[boundary]
    )
    assert result.usage.requests == len(requests)
    assert "private" not in result.model_dump_json()


async def test_model_route_change_keeps_old_result_and_attempt_identity() -> None:
    adapter, context, guard = build(lambda request: response())
    first = await adapter.run_typed("investigation-v1", context, (), Balance, LLMRequestPolicy())
    frozen = first.model_dump_json()
    adapter.settings = adapter.settings.model_copy(update={"model_route": "investigation-v2"})
    rejected = await adapter.run_typed("investigation-v1", context, (), Balance, LLMRequestPolicy())
    second = await adapter.run_typed("investigation-v2", context, (), Balance, LLMRequestPolicy())
    assert rejected.error.code.value == "AUTH_EXPIRED" and not rejected.provider_attempted
    assert first.model_dump_json() == frozen and first.model_route == "investigation-v1"
    assert second.model_route == "investigation-v2" and first.attempt_id != second.attempt_id
    assert first.attempts[0].attempt_id != second.attempts[0].attempt_id
    assert len(guard.before) == 2


async def test_real_read_timeout_retains_failed_ledger_and_no_second_request() -> None:
    import asyncio

    async def reply(request: Any) -> Any:
        return response(
            output=[
                {
                    "type": "function_call",
                    "id": "fc_test",
                    "call_id": "call_test",
                    "name": "get_invoice",
                    "arguments": '{"invoice":"INV-1"}',
                    "status": "completed",
                }
            ]
        )

    async def slow_read(args: Any) -> Any:
        await asyncio.sleep(5)
        return Balance(amount=0)

    adapter, context, guard = build(reply)
    result = await adapter.run_typed(
        "investigation-v1",
        context,
        (ReadTool("get_invoice", Lookup, Balance, slow_read),),
        Balance,
        LLMRequestPolicy(timeout_seconds=1),
    )
    assert result.output is None and result.error.code.value == "TEMPORARILY_UNAVAILABLE"
    assert result.usage.requests == 1 and len(guard.after) == 1
    assert len(result.tools) == 1 and result.tools[0].output is None
    assert result.tools[0].error.code.value == "TEMPORARILY_UNAVAILABLE"


@pytest.mark.parametrize(
    "reported_usage,expected_input,expected_output",
    [
        (None, None, None),
        (["unusable"], None, None),
        ({"input_tokens": "110", "output_tokens": 21}, None, 21),
        ({"input_tokens": 110, "output_tokens": -1}, 110, None),
    ],
)
async def test_unusable_usage_persists_uncertain_billing_and_retains_exposure(
    reported_usage: Any, expected_input: Any, expected_output: Any
) -> None:
    class ExposureGuard(RecordingGuard):
        def __init__(self) -> None:
            super().__init__()
            self.retained: dict[Any, tuple[int, int]] = {}

        async def before_request(self, intent: Any) -> Any:
            await super().before_request(intent)
            self.retained[intent.attempt_id] = (intent.input_token_bound, intent.output_token_bound)

        async def after_request(self, intent: Any, observation: Any) -> Any:
            await super().after_request(intent, observation)
            if observation.error is None:
                self.retained.pop(intent.attempt_id)

    requests = []

    async def reply(request: Any) -> Any:
        requests.append(request)
        body = response(usage=False).json()
        if reported_usage is not None:
            body["usage"] = reported_usage
        return httpx.Response(200, json=body, headers={"x-request-id": "req_unsettled"})

    guard = ExposureGuard()
    adapter, context, guard = build(reply, guard=guard)
    result = await adapter.run_typed("investigation-v1", context, (), Balance, LLMRequestPolicy())
    assert len(requests) == len(guard.before) == len(guard.after) == 1
    intent, observation = guard.after[0]
    assert observation.error is not None
    assert observation.error.code.value == "UNKNOWN_SIDE_EFFECT"
    assert observation.error.may_have_side_effect and not observation.error.retryable
    assert observation.provider_reference.external_id == "resp_test"
    assert observation.model_id == "gpt-6.1-sol" and observation.request_id == "req_unsettled"
    assert observation.usage.input_tokens == expected_input
    assert observation.usage.output_tokens == expected_output
    assert guard.retained[intent.attempt_id] == (intent.input_token_bound, intent.output_token_bound)
    assert intent.input_token_bound > 0 and intent.output_token_bound > 0
    assert result.output is None and result.reconciliation_required
    assert result.error == observation.error
    assert result.attempts == (observation,)
    assert result.usage.input_tokens == expected_input and result.usage.output_tokens == expected_output
    assert result.cost.amount is None and result.cost.quality.value == "UNKNOWN"
