"""Distinct mailbox/transactional contracts; truthful unsupported capabilities, no live proof."""

import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from boundary_evidence import BoundaryObserver
from pydantic import SecretStr

from aco.core.context import TenantContext
from aco.core.evidence import VerificationRow, read_verifications, record_verification
from aco.core.types import ProviderErrorCode, ProviderReference, Role
from aco.integrations.fake.email import FakeEmailAdapter
from aco.integrations.ports.email import EmailFailure, EmailPort
from aco.integrations.ports.email_dto import NotificationSubscriptionRequest, OutboundEmail
from aco.integrations.resend.adapter import EmailContext, ResendEmailAdapter
from aco.integrations.resend.settings import ResendBinding


async def test_boundary_unsupported_capability(tmp_path: Path, boundary_observer: BoundaryObserver) -> None:
    case = next(
        row
        for row in json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]
        if row["id"] == "adapter_unsupported_not_false_success"
    )
    tenant, connection = uuid4(), uuid4()
    ctx = TenantContext(tenant_id=tenant, actor_id=uuid4(), correlation_id=uuid4(), role=Role.OWNER)
    adapter: EmailPort = FakeEmailAdapter(tenant_id=tenant, connection_id=connection)
    capabilities = await adapter.describe_capabilities()
    assert case["input"]["method"] not in capabilities.features
    with pytest.raises(EmailFailure) as caught:
        await adapter.subscribe_or_renew_notifications(
            ctx, NotificationSubscriptionRequest(operation_id=uuid4())
        )
    failure = caught.value.failure
    assert failure.code == ProviderErrorCode.UNSUPPORTED_CAPABILITY
    row = VerificationRow(
        task="T26",
        requirements=case["requirements"],
        command="actual offline EmailPort invocation",
        lane="offline",
        result="PASS",
        environment="contract-offline",
        commit="local-test",
        usage={},
        cost_quality="SIMULATED",
        cost_amount="0",
        currency="USD",
    )
    ledger = tmp_path / "verification.sqlite3"
    record_verification(row, path=ledger)
    persisted = read_verifications(ledger)[0]
    boundary_observer.assert_observed(
        case["id"],
        {
            "error": failure.code.value,
            "claimed_effect": failure.may_have_side_effect,
            "essential_live_gate_if_required": persisted["required_live_result"],
        },
    )


async def test_second_email_shape_is_independent_and_truthfully_labeled() -> None:
    tenant, fake_id, resend_id = uuid4(), uuid4(), uuid4()
    ctx = TenantContext(tenant_id=tenant, actor_id=uuid4(), correlation_id=uuid4(), role=Role.OWNER)
    fake: EmailPort = FakeEmailAdapter(tenant_id=tenant, connection_id=fake_id)
    wire: list[str] = []

    async def authority(context: EmailContext, operation: str, material: object | None) -> None:
        assert context.tenant_id == tenant

    def transport(request: httpx.Request) -> httpx.Response:
        wire.append(str(request.url))
        pytest.fail("Unsupported lifecycle must not submit a provider request")

    transactional: EmailPort = ResendEmailAdapter(
        ResendBinding(
            tenant_id=tenant,
            connection_id=resend_id,
            api_key=SecretStr("offline-placeholder"),
            inbox_addresses=("inbox@example.test",),
            download_hosts=frozenset({"source.example.test"}),
            evidence_mode="MOCKED_HTTP",
        ),
        authority=authority,
        enabled=True,
        transport=httpx.MockTransport(transport),
    )
    assert type(fake) is not type(transactional)
    mailbox_capabilities, transactional_capabilities = (
        await fake.describe_capabilities(),
        await transactional.describe_capabilities(),
    )
    assert "fetch_thread" in mailbox_capabilities.features
    assert "fetch_thread" not in transactional_capabilities.features
    sent = await fake.send_email(
        ctx,
        OutboundEmail(
            tenant_id=tenant,
            provider_connection_id=fake_id,
            sender="inbox@example.test",
            to=("buyer@example.test",),
            subject="Synthetic contract invoice",
            text_body="Offline mailbox-shaped provider",
            origin_invoice_id=uuid4(),
            conversation_thread_id=uuid4(),
            operation_id=uuid4(),
            message_id="<contract@example.test>",
        ),
    )
    assert sent.synthetic is True and sent.native_identity == "SIMULATED"
    page = await fake.sync_messages(ctx)
    assert len(page.value.messages) == 1 and page.value.messages[0].message_id == "<contract@example.test>"
    assert page.value.next_cursor is not None
    message = page.value.messages[0]
    assert message.provider_thread_ref is not None
    thread = await fake.fetch_thread(ctx, message.provider_thread_ref)
    assert tuple(row.provider_ref for row in thread.value) == (message.provider_ref,)
    continuation = await fake.sync_messages(ctx, page.value.next_cursor)
    assert continuation.value.messages == ()
    with pytest.raises(EmailFailure) as cross_tenant:
        await fake.get_message(ctx.model_copy(update={"tenant_id": uuid4()}), message.provider_ref)
    assert cross_tenant.value.failure.may_have_side_effect is False
    foreign_cursor = page.value.next_cursor.model_copy(update={"provider_connection_id": resend_id})
    with pytest.raises(EmailFailure):
        await fake.sync_messages(ctx, foreign_cursor)
    with pytest.raises(EmailFailure) as caught:
        await transactional.fetch_thread(
            ctx,
            ProviderReference(provider="resend", connection_id=resend_id, external_id="unsupported-thread"),
        )
    assert caught.value.failure.code == ProviderErrorCode.UNSUPPORTED_CAPABILITY
    assert caught.value.failure.may_have_side_effect is False and wire == []
    await fake.disconnect(ctx)
    assert (await fake.validate_connection(ctx)).value.connected is False
    with pytest.raises(EmailFailure) as disconnected:
        await fake.get_message(ctx, message.provider_ref)
    assert disconnected.value.failure.code == ProviderErrorCode.AUTH_EXPIRED
    assert disconnected.value.failure.may_have_side_effect is False
    assert sent.synthetic and sent.provider_ref.connection_id == fake_id
