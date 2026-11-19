"""Exact HTTPS boundary and pessimistic exposure bounds for one explicit smoke run."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx2 as httpx

from aco.integrations.provider_smoke.config import SmokeBlocked, SmokeConfig

# Official standard global text rates, retrieved 2026-11-18. No account availability claim.
# https://developers.openai.com/api/docs/pricing
PRICE_CHECKED_AT = datetime(2026, 11, 18, tzinfo=UTC)
PRICE_SOURCE = "https://developers.openai.com/api/docs/pricing"
RATES = {
    "gpt-6.2-astra": (Decimal("8"), Decimal("40")),
    "gpt-6.2-sol": (Decimal("2"), Decimal("10")),
    "gpt-6-astra": (Decimal("10"), Decimal("50")),
    "gpt-6.1-sol": (Decimal("2"), Decimal("10")),
}


def price_bound(config: SmokeConfig, *, now: datetime) -> Decimal:
    if config.model not in RATES or not timedelta(0) <= now - PRICE_CHECKED_AT <= timedelta(days=7):
        raise SmokeBlocked("VERIFIED_CURRENT_PRICE_REQUIRED")
    input_rate, output_rate = RATES[config.model]
    # Cache writes can cost 2.5x input. Reserve that ceiling even though this smoke
    # never requests caching. Actual charge stays UNKNOWN; this is only exposure.
    bound = (
        config.max_input_tokens * input_rate * Decimal("2.5") + config.max_output_tokens * output_rate
    ) / 1_000_000
    if bound > config.max_usd:
        raise SmokeBlocked("SPEND_BOUND_EXCEEDS_CAP")
    return bound


class SmokeTransport(httpx.AsyncBaseTransport):
    def __init__(self, config: SmokeConfig, operation: str, *, inner: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self.operation = operation
        self.inner = inner or httpx.AsyncHTTPTransport(retries=0, trust_env=False)
        self.requests = 0
        self.model_reads = 0
        self.model_turns = 0
        self.input_bound = 0
        self.output_bound = 0
        self.statuses: list[int] = []
        self.request_ids: list[str] = []
        self.response_usage: list[dict[str, object]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        c, url = self.config, request.url
        if (
            url.scheme != "https"
            or url.port not in (None, 443)
            or url.query
            or url.fragment
            or url.username
            or url.password
        ):
            raise SmokeBlocked("ENDPOINT_NOT_ALLOWED")
        if self.requests >= c.max_requests:
            raise SmokeBlocked("REQUEST_CAP")
        # Recheck the private-file kill switch before each attempted wire request.
        from aco.integrations.provider_smoke.config import AUTHORIZATION_REF, load_config

        current = load_config(c.source_path, authorization_ref=AUTHORIZATION_REF)
        if current != c:
            raise SmokeBlocked("CONFIG_CHANGED")
        if self.operation == "openai" and url.host == "api.openai.com":
            if request.method == "GET" and url.path == f"/v1/models/{c.model}" and self.model_reads == 0:
                self.model_reads += 1
            elif request.method == "POST" and url.path == "/v1/responses" and self.model_turns < 2:
                price_bound(c, now=datetime.now(UTC))
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
                    or body.get("model") != c.model
                    or body.get("store") is not False
                    or body.get("stream", False)
                    or body.get("service_tier") != "default"
                    or any(
                        t.get("type") != "function" or t.get("name") != "get_synthetic_invoice"
                        for t in body.get("tools", [])
                    )
                ):
                    raise SmokeBlocked("UNSAFE_MODEL_REQUEST")
                output = body.get("max_output_tokens")
                # Every UTF-8 byte can be at most one token; reserve extra framing
                # overhead instead of downloading a tokenizer or adding API calls.
                input_bound = len(request.content) + 128
                if type(output) is not int or output <= 0 or self.output_bound + output > c.max_output_tokens:
                    raise SmokeBlocked("OUTPUT_TOKEN_CAP")
                if self.input_bound + input_bound > c.max_input_tokens:
                    raise SmokeBlocked("INPUT_TOKEN_CAP")
                self.input_bound += input_bound
                self.output_bound += output
                self.model_turns += 1
            else:
                raise SmokeBlocked("ENDPOINT_NOT_ALLOWED")
        elif (
            self.operation == "resend"
            and url.host == "api.resend.com"
            and request.method == "GET"
            and url.path == "/domains"
            and self.requests == 0
        ):
            pass
        elif (
            self.operation == "vapi"
            and url.host == "api.vapi.ai"
            and request.method == "GET"
            and url.path == (f"/assistant/{c.assistant_id}" if c.assistant_id else "/assistant")
            and self.requests == 0
        ):
            pass
        else:
            raise SmokeBlocked("ENDPOINT_NOT_ALLOWED")
        self.requests += 1
        response = await self.inner.handle_async_request(request)
        self.statuses.append(response.status_code)
        ref = response.headers.get("x-request-id") or response.headers.get("request-id")
        if ref and len(ref) < 200 and all(ch.isalnum() or ch in "_-" for ch in ref):
            self.request_ids.append(ref)
        if 300 <= response.status_code < 400:
            await response.aclose()
            raise SmokeBlocked("REDIRECT_REFUSED")
        if self.operation == "openai" and request.method == "POST" and response.status_code == 200:
            await response.aread()
            try:
                payload = response.json()
                usage = payload.get("usage", {})
                from aco.integrations.provider_smoke.runner import safe_id

                item: dict[str, object] = {
                    "response_id": safe_id(payload.get("id")),
                    "model_id": safe_id(payload.get("model")),
                }
                for key in ("input_tokens", "output_tokens", "total_tokens"):
                    value = usage.get(key)
                    item[key] = value if type(value) is int and value >= 0 else None
                self.response_usage.append(item)
            except (ValueError, TypeError, AttributeError):
                self.response_usage.append({"usage": "UNKNOWN"})
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()
