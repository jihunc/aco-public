"""One-run HTTPS boundary; a mandatory neutral guard owns durable budget/authority."""

import json
import re
from datetime import UTC, datetime
from typing import Any, NoReturn
from uuid import UUID, uuid4

import httpx2 as httpx

from aco.core.types import ProviderErrorCode, ProviderFailure, ProviderReference
from aco.integrations.ports.llm import (
    LLMExecutionBlocked,
    LLMExecutionGuard,
    LLMInput,
    LLMProviderAttempt,
    LLMRequestIntent,
    LLMRequestPolicy,
    LLMUsage,
)


class AdapterBlocked(Exception):
    def __init__(self, code: ProviderErrorCode):
        self.code = code
        super().__init__(code.value)


def safe_id(value: Any) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", value) else None


def count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def status_failure(status: int | None, request_id: str | None = None) -> ProviderFailure | None:
    if status is not None and 200 <= status < 300:
        return None
    if status in (401, 403):
        code = ProviderErrorCode.AUTH_EXPIRED
    elif status == 429:
        code = ProviderErrorCode.RATE_LIMITED
    elif status is not None and 300 <= status < 500:
        code = ProviderErrorCode.VALIDATION_FAILED
    else:
        code = ProviderErrorCode.UNKNOWN_SIDE_EFFECT
    return ProviderFailure(
        code=code,
        retryable=code == ProviderErrorCode.RATE_LIMITED,
        may_have_side_effect=code == ProviderErrorCode.UNKNOWN_SIDE_EFFECT,
        request_id=request_id,
    )


class OpenAITransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        *,
        context: LLMInput,
        run_id: UUID,
        model_id: str,
        route: str,
        policy: LLMRequestPolicy,
        guard: LLMExecutionGuard,
        tool_names: frozenset[str],
        inner: httpx.AsyncBaseTransport,
    ):
        self.context, self.run_id, self.model_id, self.route = context, run_id, model_id, route
        self.policy, self.guard, self.tool_names, self.inner = policy, guard, tool_names, inner
        self.attempts: list[LLMProviderAttempt] = []
        self.input_bound = self.output_bound = 0
        self.reconciliation_required = False
        self.blocked: ProviderErrorCode | None = None

    def deny(self, code: ProviderErrorCode = ProviderErrorCode.VALIDATION_FAILED) -> NoReturn:
        self.blocked = code
        raise AdapterBlocked(code)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url, p = request.url, self.policy
        if (
            str(url) != "https://api.openai.com/v1/responses"
            or request.method != "POST"
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            self.deny()
        body = json.loads(request.content)
        allowed = {
            "model",
            "input",
            "instructions",
            "tools",
            "text",
            "max_output_tokens",
            "store",
            "stream",
            "service_tier",
            "parallel_tool_calls",
            "tool_choice",
            "reasoning",
            "include",
        }
        if (
            set(body) - allowed
            or body.get("model") != self.model_id
            or body.get("store") is not False
            or body.get("stream", False)
            or body.get("service_tier") != "default"
            or any(
                t.get("type") != "function" or t.get("name") not in self.tool_names
                for t in body.get("tools", [])
            )
        ):
            self.deny()
        input_bound, output_bound = len(request.content) + 128, body.get("max_output_tokens")
        if (
            len(self.attempts) >= p.max_requests
            or type(output_bound) is not int
            or output_bound < 1
            or self.input_bound + input_bound > p.max_input_tokens
            or self.output_bound + output_bound > p.max_output_tokens
        ):
            self.deny()
        intent = LLMRequestIntent(
            tenant_id=self.context.tenant_id,
            connection_id=self.context.connection_id,
            actor_id=self.context.actor_id,
            correlation_id=self.context.correlation_id,
            run_attempt_id=self.run_id,
            attempt_id=uuid4(),
            model_route=self.route,
            model_id=self.model_id,
            prompt_version=self.context.prompt_version,
            schema_version=self.context.schema_version,
            input_token_bound=input_bound,
            output_token_bound=output_bound,
            occurred_at=datetime.now(UTC),
        )
        try:
            await self.guard.before_request(intent)
        except LLMExecutionBlocked as exc:
            self.deny(exc.code)
        except Exception:
            self.deny(ProviderErrorCode.TEMPORARILY_UNAVAILABLE)
        # From here, timeout/connection failures retain unknown possible billing exposure.
        self.input_bound += input_bound
        self.output_bound += output_bound
        observation = LLMProviderAttempt(
            attempt_id=intent.attempt_id, usage=LLMUsage(requests=1), error=status_failure(None)
        )
        self.attempts.append(observation)
        response = None
        try:
            response = await self.inner.handle_async_request(request)
            ref = safe_id(response.headers.get("x-request-id"))
            observation = observation.model_copy(
                update={
                    "http_status": response.status_code,
                    "request_id": ref,
                    "error": status_failure(response.status_code, ref),
                }
            )
            self.attempts[-1] = observation
            await response.aread()
            if response.status_code == 200:
                try:
                    value = response.json()
                    reported_usage = value.get("usage")
                    usage = reported_usage if isinstance(reported_usage, dict) else {}
                    reported_details = usage.get("input_tokens_details")
                    details = reported_details if isinstance(reported_details, dict) else {}
                    native = safe_id(value.get("id"))
                    observation = observation.model_copy(
                        update={
                            "model_id": safe_id(value.get("model")),
                            "provider_reference": ProviderReference(
                                provider="openai",
                                connection_id=self.context.connection_id,
                                external_id=native,
                            )
                            if native
                            else None,
                            "usage": LLMUsage(
                                requests=1,
                                input_tokens=count(usage.get("input_tokens")),
                                output_tokens=count(usage.get("output_tokens")),
                                cached_input_tokens=count(details.get("cached_tokens")),
                            ),
                        }
                    )
                    if observation.usage.input_tokens is None or observation.usage.output_tokens is None:
                        # A successful HTTP response can still leave billable exposure unknown.
                        # Persist this uncertainty before any guard can settle the reservation.
                        observation = observation.model_copy(
                            update={"error": status_failure(None, observation.request_id)}
                        )
                    self.attempts[-1] = observation
                except (ValueError, TypeError, AttributeError):
                    self.deny()
        except BaseException:
            if response is not None and observation.error is None:
                observation = observation.model_copy(
                    update={"error": status_failure(None, observation.request_id)}
                )
                self.attempts[-1] = observation
            raise
        finally:
            if self.attempts[-1].error and self.attempts[-1].error.may_have_side_effect:
                self.reconciliation_required = True
            try:
                await self.guard.after_request(intent, self.attempts[-1])
            except BaseException:
                self.reconciliation_required = True
                # Preserve the in-memory evidence and stop; never replay a paid request.
                self.deny(ProviderErrorCode.TEMPORARILY_UNAVAILABLE)
        assert response is not None
        if response.status_code == 200:
            if observation.error is not None:
                self.deny(observation.error.code)
            if (
                observation.model_id != self.model_id
                or observation.provider_reference is None
                or observation.usage.input_tokens is None
                or observation.usage.output_tokens is None
            ):
                self.deny()
            if observation.usage.input_tokens > input_bound or observation.usage.output_tokens > output_bound:
                self.reconciliation_required = True
                self.deny()
        if 300 <= response.status_code < 400:
            self.deny()
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()
