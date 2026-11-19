"""Bounded synthetic feasibility only; not the business LLM adapter or P-LLM-01."""

from datetime import UTC, datetime
from importlib.metadata import version
from typing import Any

import httpx2
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models.openai import OpenAIResponsesModel, OpenAIResponsesModelSettings
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import UsageLimits

from aco.integrations.provider_smoke.config import SmokeBlocked, SmokeConfig
from aco.integrations.provider_smoke.transport import PRICE_SOURCE, SmokeTransport, price_bound


class InvoiceCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    balance: int


async def run_preflight(config: SmokeConfig, transport: SmokeTransport) -> dict[str, Any]:
    if not config.openai_api_key.get_secret_value() or not config.model:
        raise SmokeBlocked("MODEL_AND_KEY_REQUIRED")
    ledger: list[dict[str, int]] = []

    def get_synthetic_invoice() -> dict[str, int]:
        """Read synthetic invoice balance in AUD minor units."""
        if ledger:
            raise SmokeBlocked("READ_TOOL_CAP")
        value = {"balance": 12000}
        ledger.append(value)
        return value

    async with httpx2.AsyncClient(
        transport=transport, trust_env=False, follow_redirects=False, timeout=20
    ) as http:
        client = AsyncOpenAI(
            api_key=config.openai_api_key.get_secret_value(),
            base_url="https://api.openai.com/v1",
            http_client=http,
            max_retries=0,
            timeout=20,
        )
        model = await client.models.retrieve(config.model)
        if model.id != config.model:
            raise SmokeBlocked("MODEL_ID_MISMATCH")
        bound = price_bound(config, now=datetime.now(UTC))
        settings = OpenAIResponsesModelSettings(
            max_tokens=config.max_output_tokens // 2,
            openai_store=False,
            openai_prompt_cache_diagnostics=False,
            openai_service_tier="default",
            parallel_tool_calls=False,
        )
        agent = Agent(
            OpenAIResponsesModel(config.model, provider=OpenAIProvider(openai_client=client)),
            output_type=NativeOutput(InvoiceCheck, name="Check"),
            tools=[get_synthetic_invoice],
            retries=0,
            model_settings=settings,
            tool_timeout=2,
        )
        agent.instrument = False
        result = await agent.run(
            "Call get_synthetic_invoice once; return balance.",
            usage_limits=UsageLimits(
                request_limit=2,
                tool_calls_limit=1,
                input_tokens_limit=config.max_input_tokens,
                output_tokens_limit=config.max_output_tokens,
            ),
        )
        if ledger != [{"balance": 12000}] or result.output.balance != 12000:
            raise SmokeBlocked("TYPED_TOOL_PROOF_FAILED")
        responses = [m for m in result.all_messages() if isinstance(m, ModelResponse)]
        if any(m.model_name != config.model for m in responses):
            raise SmokeBlocked("RESPONSE_MODEL_MISMATCH")
        if not transport.response_usage or any(
            row.get("response_id") is None
            or row.get("input_tokens") is None
            or row.get("output_tokens") is None
            for row in transport.response_usage
        ):
            raise SmokeBlocked("OBSERVED_USAGE_REQUIRED")
        usage = result.usage
        return {
            "status": "PASS",
            "scope": "synthetic_model_tool_feasibility_only",
            "model_id": model.id,
            "provider_response_ids": [m.provider_response_id for m in responses],
            "safe_tool_results": ledger,
            "typed_output": result.output.model_dump(),
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "cost_quality": "UNKNOWN",
            "cost_amount": None,
            "currency": "USD",
            "reserved_upper_bound_usd": str(bound),
            "price_source": PRICE_SOURCE,
            "sdk_versions": {"openai": version("openai"), "pydantic-ai-slim": version("pydantic-ai-slim")},
        }
