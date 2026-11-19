"""LLM capability/scope refusals precede tools; fakes never acquire live IO."""

from typing import Any
from uuid import uuid4

from llm_support import Balance, Lookup, setup_adapter

from aco.integrations.ports.llm import LLMRequestPolicy, ReadTool


async def test_fake_run_calls_validated_read_and_returns_immutable_provenance() -> None:
    adapter, context = setup_adapter()
    seen = []

    async def lookup(args: Any) -> Any:
        seen.append(args.invoice)
        return Balance(amount=12000)

    result = await adapter.run_typed(
        "investigation-v1",
        context,
        (ReadTool("get_invoice", Lookup, Balance, lookup),),
        Balance,
        LLMRequestPolicy(),
    )
    assert result.output == Balance(amount=12000)
    assert seen == ["INV-1"]
    assert result.tools[0].output == Balance(amount=12000)
    assert result.model_dump()["tools"][0]["output"] == {"amount": 12000}
    assert result.tenant_id == context.tenant_id
    assert result.model_id == "fake-scripted"
    assert result.authenticity == "FAKE"
    assert result.provider_attempted is False
    assert result.provider_reference is None
    assert result.cost.amount is None
    assert result.cost.quality.value == "UNKNOWN"
    assert result.schema_version == "balance-v1"
    assert result.prompt_version == "investigate-v1"


async def test_missing_capability_foreign_tenant_and_changed_route_never_call_tools() -> None:
    for failure in ("capability", "tenant", "route"):
        adapter, context = setup_adapter(
            capabilities=frozenset()
            if failure == "capability"
            else frozenset({"structured_output", "tool_calling"})
        )
        called: list[Any] = []

        async def lookup(args: Any, called: Any = called) -> Any:
            called.append(args)
            return Balance(amount=1)

        if failure == "tenant":
            context = context.model_copy(update={"tenant_id": uuid4()})
        result = await adapter.run_typed(
            "other-route" if failure == "route" else "investigation-v1",
            context,
            (ReadTool("get_invoice", Lookup, Balance, lookup),),
            Balance,
            LLMRequestPolicy(),
        )
        assert result.output is None
        assert result.error is not None
        assert result.tools == ()
        assert called == []


async def test_invalid_model_output_is_failure_and_never_fake_success() -> None:
    adapter, context = setup_adapter(response='{"amount":"paid"}')

    async def lookup(args: Any) -> Any:
        return Balance(amount=12000)

    result = await adapter.run_typed(
        "investigation-v1",
        context,
        (ReadTool("get_invoice", Lookup, Balance, lookup),),
        Balance,
        LLMRequestPolicy(),
    )
    assert result.output is None
    assert result.error.code.value == "VALIDATION_FAILED"
    assert len(result.tools) == 1
    assert result.provider_attempted is False


async def test_fake_refuses_mutation_tools_and_tool_budget_before_invocation() -> None:
    adapter, context = setup_adapter()
    called: list[Any] = []

    async def action(args: Any) -> Any:
        called.append(args)
        return Balance(amount=0)

    for name, limit in (("send_email", 2), ("get_invoice", 0)):
        result = await adapter.run_typed(
            "investigation-v1",
            context,
            (ReadTool(name, Lookup, Balance, action),),
            Balance,
            LLMRequestPolicy(max_tool_calls=limit),
        )
        assert result.error is not None
        assert result.output is None
    assert called == []


async def test_failed_read_retains_failure_ledger_without_fabricated_result() -> None:
    adapter, context = setup_adapter()

    async def unavailable(args: Any) -> Any:
        raise RuntimeError("secret provider failure")

    result = await adapter.run_typed(
        "investigation-v1",
        context,
        (ReadTool("get_invoice", Lookup, Balance, unavailable),),
        Balance,
        LLMRequestPolicy(),
    )
    assert result.output is None
    assert result.error.code.value == "TEMPORARILY_UNAVAILABLE"
    assert len(result.tools) == 1
    assert result.tools[0].output is None
    assert result.tools[0].error.code.value == "TEMPORARILY_UNAVAILABLE"
    assert "secret" not in str(result)
