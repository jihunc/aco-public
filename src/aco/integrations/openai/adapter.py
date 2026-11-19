"""Maintained SDK/PydanticAI adapter. All authority and durable records belong to composition."""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import httpx2 as httpx
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError
from pydantic_ai import Agent, NativeOutput, RunContext, Tool
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.models.openai import OpenAIResponsesModel, OpenAIResponsesModelSettings
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import UsageLimits

from aco.core.types import CostQuality, ProviderCapabilities, ProviderCost, ProviderErrorCode, ProviderFailure
from aco.integrations.openai.settings import OpenAISettings
from aco.integrations.openai.transport import AdapterBlocked, OpenAITransport
from aco.integrations.ports.llm import (
    READ_TOOL_NAMES,
    LLMExecutionBlocked,
    LLMExecutionGuard,
    LLMInput,
    LLMRequestPolicy,
    LLMUsage,
    ReadTool,
    ToolReadEvidence,
    TypedResult,
)


class OpenAIModelAdapter:
    def __init__(
        self,
        *,
        tenant_id: UUID,
        connection_id: UUID,
        settings: OpenAISettings,
        execution_guard: LLMExecutionGuard | None,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    ):
        self.tenant_id, self.connection_id, self.settings = tenant_id, connection_id, settings
        self.execution_guard = execution_guard
        self._transport_factory = transport_factory or (
            lambda: httpx.AsyncHTTPTransport(retries=0, trust_env=False)
        )

    async def describe_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            features=self.settings.features & {"structured_output", "tool_calling"},
            limits={
                "max_requests": 4,
                "max_tool_calls": 8,
                "max_input_tokens": 64000,
                "max_output_tokens": 4096,
            },
        )

    async def run_typed[OutputT: BaseModel](
        self,
        model_route: str,
        input_context: LLMInput,
        tools: tuple[ReadTool[Any, Any], ...],
        output_schema: type[OutputT],
        request_policy: LLMRequestPolicy,
    ) -> TypedResult[OutputT]:
        s, p = self.settings, request_policy
        run_id, occurred_at = uuid4(), datetime.now(UTC)
        ledger: list[ToolReadEvidence] = []
        gate: OpenAITransport | None = None
        output: OutputT | None = None
        code: ProviderErrorCode | None = None
        if (
            input_context.tenant_id != self.tenant_id
            or input_context.connection_id != self.connection_id
            or model_route != s.model_route
        ):
            code = ProviderErrorCode.AUTH_EXPIRED
        elif (
            "structured_output" not in s.features
            or (tools and "tool_calling" not in s.features)
            or any(t.name not in READ_TOOL_NAMES for t in tools)
        ):
            code = ProviderErrorCode.UNSUPPORTED_CAPABILITY
        elif (
            self.execution_guard is None
            or not s.api_key.get_secret_value().strip()
            or len({t.name for t in tools}) != len(tools)
            or (tools and p.max_tool_calls == 0)
            or p.max_output_tokens < p.max_requests
        ):
            code = ProviderErrorCode.VALIDATION_FAILED

        def make_tool(tool: ReadTool[Any, Any]) -> Tool[Any]:
            async def invoke(ctx: RunContext[Any], **kwargs: Any) -> BaseModel:
                if len(ledger) >= p.max_tool_calls:
                    raise AdapterBlocked(ProviderErrorCode.VALIDATION_FAILED)
                arguments = tool.input_schema.model_validate_json(json.dumps(kwargs), strict=True)
                try:
                    # The trusted closure must freshly authorize this scoped read.
                    value = await tool.invoke(arguments)
                    value = tool.output_schema.model_validate(value, strict=True)
                except BaseException as exc:
                    read_code = (
                        exc.code
                        if isinstance(exc, LLMExecutionBlocked)
                        else ProviderErrorCode.VALIDATION_FAILED
                        if isinstance(exc, ValidationError)
                        else ProviderErrorCode.TEMPORARILY_UNAVAILABLE
                    )
                    ledger.append(
                        ToolReadEvidence(
                            name=tool.name,
                            call_id=ctx.tool_call_id,
                            request_attempt_id=gate.attempts[-1].attempt_id
                            if gate and gate.attempts
                            else None,
                            input=arguments,
                            output=None,
                            error=ProviderFailure(
                                code=read_code,
                                retryable=False,
                                may_have_side_effect=False,
                            ),
                        )
                    )
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    raise AdapterBlocked(read_code) from None
                ledger.append(
                    ToolReadEvidence(
                        name=tool.name,
                        input=arguments,
                        output=value,
                        call_id=ctx.tool_call_id,
                        request_attempt_id=gate.attempts[-1].attempt_id if gate and gate.attempts else None,
                    )
                )
                return cast(BaseModel, value)

            return Tool.from_schema(
                invoke,
                name=tool.name,
                description="Authorized scoped read only.",
                json_schema=tool.input_schema.model_json_schema(),
                sequential=True,
                takes_ctx=True,
            )

        if code is None:
            assert self.execution_guard is not None
            gate = OpenAITransport(
                context=input_context,
                run_id=run_id,
                model_id=s.model_id,
                route=s.model_route,
                policy=p,
                guard=self.execution_guard,
                tool_names=frozenset(t.name for t in tools),
                inner=self._transport_factory(),
            )
            try:
                async with (
                    asyncio.timeout(p.timeout_seconds),
                    httpx.AsyncClient(
                        transport=gate, trust_env=False, follow_redirects=False, timeout=p.timeout_seconds
                    ) as http,
                ):
                    client = AsyncOpenAI(
                        api_key=s.api_key.get_secret_value(),
                        base_url="https://api.openai.com/v1",
                        http_client=http,
                        max_retries=0,
                        timeout=p.timeout_seconds,
                    )
                    agent = Agent(
                        OpenAIResponsesModel(s.model_id, provider=OpenAIProvider(openai_client=client)),
                        output_type=NativeOutput(output_schema),
                        tools=[make_tool(t) for t in tools],
                        retries=0,
                        model_settings=OpenAIResponsesModelSettings(
                            max_tokens=p.max_output_tokens // p.max_requests,
                            openai_store=False,
                            openai_prompt_cache_diagnostics=False,
                            openai_service_tier="default",
                            # Models may batch safe reads; maintained Tool sequential=True
                            # still reloads each guarded closure serially.
                            parallel_tool_calls=True,
                        ),
                        tool_timeout=p.timeout_seconds,
                    )
                    agent.instrument = False
                    result = await agent.run(
                        input_context.text,
                        usage_limits=UsageLimits(
                            request_limit=p.max_requests,
                            tool_calls_limit=p.max_tool_calls,
                            input_tokens_limit=p.max_input_tokens,
                            output_tokens_limit=p.max_output_tokens,
                        ),
                    )
                    output = output_schema.model_validate(result.output, strict=True)
            except AdapterBlocked as exc:
                code = exc.code
            except (ValidationError, UnexpectedModelBehavior, UsageLimitExceeded):
                code = ProviderErrorCode.VALIDATION_FAILED
            except Exception:
                code = ProviderErrorCode.TEMPORARILY_UNAVAILABLE
        attempts = tuple(gate.attempts) if gate else ()
        if gate and gate.blocked:
            code = gate.blocked
        failure = None
        if code is not None:
            output = None
            observed_failure = attempts[-1].error if attempts else None
            if observed_failure is not None and (
                observed_failure.may_have_side_effect or not (gate and gate.blocked)
            ):
                failure = observed_failure
            else:
                failure = ProviderFailure(
                    code=code,
                    retryable=False,
                    may_have_side_effect=code == ProviderErrorCode.UNKNOWN_SIDE_EFFECT,
                )

        def total(name: str) -> int | None:
            values = [getattr(a.usage, name) for a in attempts]
            return sum(values) if values and all(v is not None for v in values) else None

        refs = [a.provider_reference for a in attempts if a.provider_reference is not None]
        return TypedResult[OutputT](
            tenant_id=input_context.tenant_id,
            connection_id=input_context.connection_id,
            actor_id=input_context.actor_id,
            correlation_id=input_context.correlation_id,
            attempt_id=run_id,
            occurred_at=occurred_at,
            model_route=s.model_route,
            model_id=s.model_id,
            prompt_version=input_context.prompt_version,
            schema_version=input_context.schema_version,
            authenticity="LIVE",
            provider_attempted=bool(attempts),
            provider_reference=refs[-1] if refs else None,
            output=output,
            error=failure,
            tools=tuple(ledger),
            attempts=attempts,
            reconciliation_required=gate.reconciliation_required if gate else False,
            usage=LLMUsage(
                requests=len(attempts),
                input_tokens=total("input_tokens"),
                output_tokens=total("output_tokens"),
                cached_input_tokens=total("cached_input_tokens"),
            ),
            cost=ProviderCost(amount=None, currency="USD", quality=CostQuality.UNKNOWN),
        )
