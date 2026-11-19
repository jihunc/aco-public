"""Vendor-neutral typed investigation boundary. DTOs are not execution authority."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field, SerializeAsAny, model_validator

from aco.core.types import (
    ProviderCapabilities,
    ProviderCost,
    ProviderErrorCode,
    ProviderFailure,
    ProviderReference,
    StrictModel,
)

READ_TOOL_NAMES = frozenset(
    {
        "get_customer",
        "get_invoice",
        "list_customer_invoices",
        "get_verified_payments",
        "get_payment_records",
        "get_communication_history",
        "find_existing_cases",
        "find_collection_cases",
        "search_case_evidence",
        "get_payment_promises",
        "list_collection_tasks",
        "get_collection_policy",
        "get_policy",
        "get_policy_and_contact_status",
    }
)


class LLMInput(StrictModel):
    tenant_id: UUID
    connection_id: UUID
    actor_id: UUID
    correlation_id: UUID
    prompt_version: Annotated[str, Field(min_length=1, max_length=100)]
    schema_version: Annotated[str, Field(min_length=1, max_length=100)]
    text: Annotated[str, Field(min_length=1, max_length=16000)]


class LLMRequestPolicy(StrictModel):
    max_requests: Annotated[int, Field(ge=1, le=4)] = 2
    max_tool_calls: Annotated[int, Field(ge=0, le=8)] = 4
    max_input_tokens: Annotated[int, Field(ge=1, le=64000)] = 4096
    max_output_tokens: Annotated[int, Field(ge=1, le=4096)] = 1024
    timeout_seconds: Annotated[int, Field(ge=1, le=60)] = 20


@dataclass(frozen=True)
class ReadTool[InputT: BaseModel, OutputT: BaseModel]:
    """Constructed only by trusted application composition with scoped read closures."""

    name: str
    input_schema: type[InputT]
    output_schema: type[OutputT]
    invoke: Callable[[InputT], Awaitable[OutputT]]


class ToolReadEvidence(StrictModel):
    name: str
    call_id: str | None = None
    request_attempt_id: UUID | None = None
    input: SerializeAsAny[BaseModel]
    output: SerializeAsAny[BaseModel] | None
    error: ProviderFailure | None = None


class LLMUsage(StrictModel):
    requests: Annotated[int, Field(ge=0)]
    input_tokens: Annotated[int, Field(ge=0)] | None = None
    output_tokens: Annotated[int, Field(ge=0)] | None = None
    cached_input_tokens: Annotated[int, Field(ge=0)] | None = None


class LLMRequestIntent(StrictModel):
    """Pre-wire exposure; guard must reserve and commit before returning."""

    tenant_id: UUID
    connection_id: UUID
    actor_id: UUID
    correlation_id: UUID
    run_attempt_id: UUID
    attempt_id: UUID
    model_route: str
    model_id: str
    prompt_version: str
    schema_version: str
    input_token_bound: Annotated[int, Field(ge=1)]
    output_token_bound: Annotated[int, Field(ge=1)]
    occurred_at: AwareDatetime


class LLMProviderAttempt(StrictModel):
    attempt_id: UUID
    provider_reference: ProviderReference | None = None
    model_id: str | None = None
    request_id: str | None = None
    http_status: int | None = None
    usage: LLMUsage
    error: ProviderFailure | None = None


class LLMExecutionBlocked(Exception):
    """Guard refusal with no provider payload or caller-controlled error text."""

    def __init__(self, code: ProviderErrorCode = ProviderErrorCode.VALIDATION_FAILED):
        self.code = code
        super().__init__(code.value)


class LLMExecutionGuard(Protocol):
    async def before_request(self, intent: LLMRequestIntent) -> None:
        """Reauthorize, check kills/rates, reserve exposure, commit intent; raise to deny."""
        ...

    async def after_request(self, intent: LLMRequestIntent, observation: LLMProviderAttempt) -> None:
        """Independently persist observed or uncertain outcome; never release unknown exposure."""
        ...


class TypedResult[OutputT: BaseModel](StrictModel):
    tenant_id: UUID
    connection_id: UUID
    actor_id: UUID
    correlation_id: UUID
    attempt_id: UUID
    occurred_at: AwareDatetime
    model_route: str
    model_id: str
    prompt_version: str
    schema_version: str
    authenticity: Literal["FAKE", "LIVE"]
    provider_attempted: bool
    provider_reference: ProviderReference | None
    output: OutputT | None
    error: ProviderFailure | None
    tools: tuple[ToolReadEvidence, ...]
    usage: LLMUsage
    cost: ProviderCost
    attempts: tuple[LLMProviderAttempt, ...] = ()
    reconciliation_required: bool = False

    @model_validator(mode="after")
    def valid_result(self) -> "TypedResult[OutputT]":
        if (self.output is None) == (self.error is None):
            raise ValueError("Exactly one validated output or failure is required")
        if self.authenticity == "FAKE" and (
            self.provider_attempted or self.provider_reference or self.attempts
        ):
            raise ValueError("Fake output cannot claim actual provider invocation")
        if self.provider_reference and self.provider_reference.connection_id != self.connection_id:
            raise ValueError("Provider reference must match the bound connection")
        return self


@runtime_checkable
class LLMPort(Protocol):
    connection_id: UUID

    async def describe_capabilities(self) -> ProviderCapabilities: ...
    async def run_typed[OutputT: BaseModel](
        self,
        model_route: str,
        input_context: LLMInput,
        tools: tuple[ReadTool[Any, Any], ...],
        output_schema: type[OutputT],
        request_policy: LLMRequestPolicy,
    ) -> TypedResult[OutputT]: ...
