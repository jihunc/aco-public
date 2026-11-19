"""Shared synthetic HTTP responses for real SDK tests; never live IO."""

from typing import Any

import httpx2 as httpx

from aco.core.types import ProviderErrorCode
from aco.integrations.ports.llm import LLMExecutionBlocked, LLMProviderAttempt, LLMRequestIntent


class RecordingGuard:
    def __init__(self, *, deny_on: int | None = None, fail_after: bool = False) -> None:
        self.before: list[LLMRequestIntent] = []
        self.after: list[tuple[LLMRequestIntent, LLMProviderAttempt]] = []
        self.deny_on = deny_on
        self.fail_after = fail_after

    async def before_request(self, intent: LLMRequestIntent) -> None:
        self.before.append(intent)
        if len(self.before) == self.deny_on:
            raise LLMExecutionBlocked(ProviderErrorCode.VALIDATION_FAILED)

    async def after_request(self, intent: LLMRequestIntent, observation: LLMProviderAttempt) -> None:
        self.after.append((intent, observation))
        if self.fail_after:
            raise RuntimeError("private persistence details")


def response(
    *, output: Any = None, model: Any = "gpt-6.1-sol", response_id: Any = "resp_test", usage: Any = True
) -> Any:
    value = {
        "id": response_id,
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "model": model,
        "parallel_tool_calls": False,
        "tools": [],
        "tool_choice": "auto",
        "output": output
        if output is not None
        else [
            {
                "type": "message",
                "id": "msg_test",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": '{"amount":12000}', "annotations": []}],
            }
        ],
    }
    if usage:
        value["usage"] = {
            "input_tokens": 110,
            "output_tokens": 21,
            "total_tokens": 131,
            "input_tokens_details": {"cached_tokens": 7},
            "output_tokens_details": {"reasoning_tokens": 0},
        }
    return httpx.Response(200, json=value, headers={"x-request-id": "req_test"})
