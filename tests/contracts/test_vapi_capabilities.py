"""Binding-specific operation support is metadata, never current LIVE authority."""

from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from aco.integrations.ports.voice import CallContext
from aco.integrations.vapi.adapter import VapiVoiceAdapter
from aco.integrations.vapi.settings import VapiBinding


@pytest.mark.parametrize(
    "mode,control_key,control_hosts",
    [
        ("MOCKED_HTTP", True, True),
        ("LIVE", False, False),
        ("LIVE", True, False),
        ("LIVE", False, True),
        ("LIVE", True, True),
    ],
)
async def test_vapi_capabilities_describe_binding_support_without_readiness_or_wire(
    mode: str, control_key: bool, control_hosts: bool
) -> None:
    async def forbidden_authority(context: CallContext, operation: str, material: object) -> None:
        pytest.fail("Reading capability support cannot acquire action authority")

    def forbidden_wire(request: httpx.Request) -> httpx.Response:
        pytest.fail("Reading capability support cannot probe or call a provider")

    binding = VapiBinding.model_validate(
        {
            "tenant_id": uuid4(),
            "connection_id": uuid4(),
            "assistant_id": uuid4(),
            "api_key": SecretStr("explicit-offline-capability-fixture"),
            "webhook_secret": SecretStr("explicit-offline-signature-fixture"),
            "credential_revision": "synthetic-capability-metadata",
            "evidence_mode": mode,
            "max_duration_seconds": 60,
            "control_api_key": SecretStr("explicit-offline-control-fixture") if control_key else None,
            "control_hosts": ("control.vapi.ai",) if control_hosts else (),
        }
    )
    adapter = VapiVoiceAdapter(
        binding,
        authority=forbidden_authority,
        enabled=False,
        transport=httpx.MockTransport(forbidden_wire),
    )
    capabilities = await adapter.describe_capabilities()
    expected = {"get_call", "capture_disabled"}
    expected |= (
        {"mocked_browser_start", "configured_hmac_callbacks"}
        if mode == "MOCKED_HTTP"
        else {"guarded_live_browser_start", "guarded_live_hmac_callbacks"}
    )
    if mode == "LIVE" and control_key and control_hosts:
        expected.add("retained_capability_safety_stop")
    assert capabilities.features == frozenset(expected)
    assert capabilities.limits == {"max_duration_seconds": 60, "max_tool_events": 4}
    assert not capabilities.features & {"pstn", "recording", "transcript", "cancel_call", "ready"}
