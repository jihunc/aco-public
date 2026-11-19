"""Explicit scripted fake; no network dependencies, keys, fallbacks, or domain writes."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ValidationError

from aco.core.types import CostQuality, ProviderCapabilities, ProviderCost, ProviderErrorCode, ProviderFailure
from aco.integrations.ports.llm import (
    READ_TOOL_NAMES,
    LLMInput,
    LLMRequestPolicy,
    LLMUsage,
    ReadTool,
    ToolReadEvidence,
    TypedResult,
)


@dataclass(frozen=True)
class ScriptedRead:
    name: str
    arguments_json: str


class FakeLLMAdapter:
    def __init__(
        self,
        *,
        tenant_id: UUID,
        connection_id: UUID,
        route: str,
        output_json: str,
        reads: tuple[ScriptedRead, ...] = (),
        features: frozenset[str] = frozenset({"structured_output", "tool_calling"}),
    ) -> None:
        self.tenant_id = tenant_id
        self.connection_id = connection_id
        self.route = route
        self.output_json = output_json
        self.reads = reads
        self.features = features

    async def describe_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(features=self.features, limits={"max_tool_calls": 8})

    async def run_typed[OutputT: BaseModel](
        self,
        model_route: str,
        input_context: LLMInput,
        tools: tuple[ReadTool[Any, Any], ...],
        output_schema: type[OutputT],
        request_policy: LLMRequestPolicy,
    ) -> TypedResult[OutputT]:
        evidence: list[ToolReadEvidence] = []
        output: OutputT | None = None
        error = None
        code = None
        if (
            input_context.tenant_id != self.tenant_id
            or input_context.connection_id != self.connection_id
            or model_route != self.route
        ):
            code = ProviderErrorCode.AUTH_EXPIRED
        elif (
            "structured_output" not in self.features
            or (self.reads and "tool_calling" not in self.features)
            or any(tool.name not in READ_TOOL_NAMES for tool in tools)
        ):
            code = ProviderErrorCode.UNSUPPORTED_CAPABILITY
        elif (
            len(self.reads) > request_policy.max_tool_calls
            or len({t.name for t in tools}) != len(tools)
            or len(input_context.text.encode()) > request_policy.max_input_tokens
        ):
            code = ProviderErrorCode.VALIDATION_FAILED
        if code is None:
            try:
                available = {tool.name: tool for tool in tools}
                async with asyncio.timeout(request_policy.timeout_seconds):
                    for read in self.reads:
                        tool = available[read.name]
                        arguments = tool.input_schema.model_validate_json(read.arguments_json, strict=True)
                        completed = False
                        try:
                            value = await tool.invoke(arguments)
                            value = tool.output_schema.model_validate(value, strict=True)
                            evidence.append(ToolReadEvidence(name=read.name, input=arguments, output=value))
                            completed = True
                        finally:
                            if not completed:
                                evidence.append(
                                    ToolReadEvidence(
                                        name=read.name,
                                        input=arguments,
                                        output=None,
                                        error=ProviderFailure(
                                            code=ProviderErrorCode.TEMPORARILY_UNAVAILABLE,
                                            retryable=False,
                                            may_have_side_effect=False,
                                        ),
                                    )
                                )
                    output = output_schema.model_validate_json(self.output_json, strict=True)
            except (ValidationError, ValueError, KeyError):
                code = ProviderErrorCode.VALIDATION_FAILED
            except TimeoutError:
                code = ProviderErrorCode.TEMPORARILY_UNAVAILABLE
            except Exception:
                code = ProviderErrorCode.TEMPORARILY_UNAVAILABLE
        if code is not None:
            error = ProviderFailure(code=code, retryable=False, may_have_side_effect=False)
        return TypedResult[OutputT](
            tenant_id=input_context.tenant_id,
            connection_id=input_context.connection_id,
            actor_id=input_context.actor_id,
            correlation_id=input_context.correlation_id,
            attempt_id=uuid4(),
            occurred_at=datetime.now(UTC),
            model_route=self.route,
            model_id="fake-scripted",
            prompt_version=input_context.prompt_version,
            schema_version=input_context.schema_version,
            authenticity="FAKE",
            provider_attempted=False,
            provider_reference=None,
            output=output,
            error=error,
            tools=tuple(evidence),
            usage=LLMUsage(requests=0),
            cost=ProviderCost(amount=None, currency="USD", quality=CostQuality.UNKNOWN),
        )
