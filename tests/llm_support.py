"""Shared synthetic LLM contract inputs; no live provider access."""

from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from aco.integrations.fake.llm import FakeLLMAdapter, ScriptedRead
from aco.integrations.ports.llm import LLMInput


class Lookup(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    invoice: str


class Balance(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    amount: int


def setup_adapter(
    *,
    capabilities: Any = frozenset({"structured_output", "tool_calling"}),
    response: Any = '{"amount":12000}',
) -> Any:
    tenant, connection, actor, correlation = (uuid4() for _ in range(4))
    context = LLMInput(
        tenant_id=tenant,
        connection_id=connection,
        actor_id=actor,
        correlation_id=correlation,
        prompt_version="investigate-v1",
        schema_version="balance-v1",
        text="Synthetic invoice INV-1 has an unverified payment claim.",
    )
    adapter = FakeLLMAdapter(
        tenant_id=tenant,
        connection_id=connection,
        route="investigation-v1",
        output_json=response,
        reads=(ScriptedRead("get_invoice", '{"invoice":"INV-1"}'),),
        features=capabilities,
    )
    return adapter, context
