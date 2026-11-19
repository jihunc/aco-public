"""Official Resend HTTP shapes through real HTTPX/Svix; no vendor traffic or real credentials."""

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr
from svix.webhooks import Webhook

from aco.core.clock import FrozenClock
from aco.core.context import TenantContext
from aco.core.types import ProviderErrorCode, ProviderFailure, ProviderReference, Role
from aco.integrations.ports.email import EmailFailure, EmailPort
from aco.integrations.ports.email_dto import OutboundEmail
from aco.integrations.resend.adapter import ResendEmailAdapter
from aco.integrations.resend.settings import ResendBinding

AT = datetime(2026, 11, 18, 12, tzinfo=UTC)
TENANT, CONNECTION, NATIVE = uuid4(), uuid4(), uuid4()
RAW = (
    b"From: Buyer <buyer@example.test>\r\nTo: inbox@example.test\r\n"
    b"Message-ID: <reply@buyer.test>\r\nIn-Reply-To: <outbound@aco.test>\r\n"
    b"References: <first@aco.test> <outbound@aco.test>\r\nSubject: Re: invoice\r\n\r\nPromise evidence.\r\n"
)


def ctx(tenant: UUID = TENANT) -> TenantContext:
    return TenantContext(tenant_id=tenant, actor_id=uuid4(), role=Role.OWNER, correlation_id=uuid4())


def binding(**changes: Any) -> ResendBinding:
    return ResendBinding(
        tenant_id=TENANT,
        connection_id=CONNECTION,
        api_key=SecretStr("re_synthetic_not_a_key"),
        inbox_addresses=("inbox@example.test",),
        download_hosts=frozenset({"source.resend.test"}),
        webhook_secret=SecretStr("whsec_" + base64.b64encode(b"synthetic-signature-secret").decode()),
        **changes,
    )


async def authorized(context: object, operation: str, material: object | None) -> None:
    """Offline authority double; never production authority from a caller UUID."""


def received(**changes: Any) -> dict[str, Any]:
    return {
        "id": str(NATIVE),
        "from": "Buyer <buyer@example.test>",
        "to": ["inbox@example.test"],
        "cc": ["finance@example.test"],
        "bcc": [],
        "subject": "Re: invoice",
        "text": "Promise evidence.",
        "html": None,
        "created_at": AT.isoformat(),
        "message_id": "<reply@buyer.test>",
        "headers": {
            "Message-ID": "<reply@buyer.test>",
            "In-Reply-To": "<outbound@aco.test>",
            "References": "<first@aco.test> <outbound@aco.test>",
        },
        "attachments": [],
        "raw": {
            "download_url": "https://source.resend.test/original?signature=secret",
            "expires_at": (AT + timedelta(hours=1)).isoformat(),
        },
        **changes,
    }


def adapter(handler: Any, **changes: Any) -> ResendEmailAdapter:
    return ResendEmailAdapter(
        binding(),
        authority=authorized,
        enabled=True,
        clock=FrozenClock(AT),
        transport=httpx.MockTransport(handler),
        **changes,
    )


def reference() -> ProviderReference:
    return ProviderReference(provider="resend", connection_id=CONNECTION, external_id=f"received:{NATIVE}")


def outgoing() -> OutboundEmail:
    return OutboundEmail(
        tenant_id=TENANT,
        provider_connection_id=CONNECTION,
        origin_invoice_id=uuid4(),
        conversation_thread_id=uuid4(),
        operation_id=uuid4(),
        sender="collections@example.test",
        to=("buyer@example.test",),
        subject="Invoice",
        text_body="Synthetic test only",
        message_id="<outbound@aco.test>",
        in_reply_to="<first@buyer.test>",
        references=("<first@buyer.test>",),
    )


async def test_resend_received_preserves_actual_original_headers_and_native_identity() -> None:
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "api.resend.com":
            assert request.headers["authorization"] == "Bearer re_synthetic_not_a_key"
            assert request.url.path == f"/emails/receiving/{NATIVE}"
            assert request.url.params["html_format"] == "cid"
            return httpx.Response(200, json=received(), headers={"x-request-id": "native-read-1"})
        assert "authorization" not in request.headers
        return httpx.Response(200, content=RAW)

    port = adapter(handle)
    assert isinstance(port, EmailPort)
    result = await port.get_message(ctx(), reference())
    message = result.value
    assert result.synthetic is False and result.authenticity == "VERIFIED" and result.cost.amount is None
    assert message.source.kind == "PROVIDER_ORIGINAL" and message.source.content == RAW
    assert message.source.integrity_sha256 == sha256(RAW).hexdigest()
    assert message.message_id == "<reply@buyer.test>" and message.in_reply_to == "<outbound@aco.test>"
    assert message.references == ("<first@aco.test>", "<outbound@aco.test>")
    assert message.provider_thread_ref is None  # No invented Resend native thread ID.
    assert ("in-reply-to", "<outbound@aco.test>") in message.headers
    assert len(seen) == 2


async def test_received_expired_raw_url_refetches_same_native_identity_without_cloning() -> None:
    reads = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal reads
        if request.url.host == "api.resend.com":
            reads += 1
            return httpx.Response(
                200,
                json=received(
                    raw={
                        "download_url": f"https://source.resend.test/raw/{reads}",
                        "expires_at": (AT + timedelta(minutes=10)).isoformat(),
                    }
                ),
            )
        return httpx.Response(403 if reads == 1 else 200, content=RAW)

    result = await adapter(handle).get_message(ctx(), reference())
    assert result.value.provider_ref == reference() and result.value.source.content == RAW
    assert reads == 2


@pytest.mark.parametrize(
    "url",
    [
        "http://source.resend.test/raw",
        "https://127.0.0.1/raw",
        "https://api.resend.com/raw",
        "https://source.resend.test@evil.test/raw",
    ],
)
async def test_raw_url_cannot_escape_explicit_source_host_or_leak_api_credential(url: str) -> None:
    count = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(
            200,
            json=received(raw={"download_url": url, "expires_at": (AT + timedelta(minutes=10)).isoformat()}),
        )

    with pytest.raises(EmailFailure):
        await adapter(handle).get_message(ctx(), reference())
    assert count == 1


async def test_send_preserves_rfc_headers_and_stable_scoped_key_without_global_credentials() -> None:
    command = outgoing()
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        assert body["headers"] == {
            "Message-ID": command.message_id,
            "In-Reply-To": command.in_reply_to,
            "References": "<first@buyer.test>",
        }
        assert request.headers["idempotency-key"] == f"aco:{TENANT}:{CONNECTION}:{command.operation_id}"
        return httpx.Response(200, json={"id": str(NATIVE)}, headers={"x-request-id": "send-1"})

    result = await adapter(handle, send_enabled=True).send_email(ctx(), command)
    assert result.message_ref.external_id == f"sent:{NATIVE}" and result.operation_id == command.operation_id
    assert result.status == "ACCEPTED" and not result.synthetic and len(seen) == 1


async def test_default_send_gate_foreign_tenant_and_connection_make_no_request() -> None:
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": str(NATIVE)})

    port = adapter(handle)
    with pytest.raises(EmailFailure):
        await port.send_email(ctx(), outgoing())
    with pytest.raises(EmailFailure):
        await port.get_message(ctx(uuid4()), reference())
    with pytest.raises(EmailFailure):
        await port.get_message(ctx(), reference().model_copy(update={"connection_id": uuid4()}))
    assert seen == []


@pytest.mark.parametrize(
    "status,expected,effect,retry",
    [
        (401, "AUTH_EXPIRED", False, False),
        (429, "RATE_LIMITED", False, True),
        (500, "UNKNOWN_SIDE_EFFECT", True, False),
    ],
)
async def test_send_maps_errors_without_blind_retry(
    status: int, expected: str, effect: bool, retry: bool
) -> None:
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, headers={"retry-after": "11", "x-request-id": "safe-request"})

    port = adapter(handle, send_enabled=True)
    with pytest.raises(EmailFailure) as caught:
        await port.send_email(ctx(), outgoing())
    assert caught.value.failure.code.value == expected
    assert caught.value.failure.may_have_side_effect == effect and caught.value.failure.retryable == retry
    assert caught.value.failure.request_id == "safe-request" and len(seen) == 1
    if status == 429:
        assert caught.value.failure.retry_after == timedelta(seconds=11)


async def test_timeout_is_unknown_and_expired_vendor_key_cannot_override_current_operation_fence() -> None:
    command = outgoing()
    attempted = False
    count = 0

    async def fence(context: object, operation: str, material: object | None) -> None:
        if operation == "send_email" and attempted:
            raise EmailFailure(
                ProviderFailure(
                    code=ProviderErrorCode.UNKNOWN_SIDE_EFFECT, retryable=False, may_have_side_effect=True
                )
            )

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempted, count
        attempted = True
        count += 1
        raise httpx.ReadTimeout("synthetic uncertain effect", request=request)

    port = ResendEmailAdapter(
        binding(),
        authority=fence,
        enabled=True,
        send_enabled=True,
        transport=httpx.MockTransport(handle),
        clock=FrozenClock(AT),
    )
    with pytest.raises(EmailFailure) as first:
        await port.send_email(ctx(), command)
    assert first.value.failure.code == ProviderErrorCode.UNKNOWN_SIDE_EFFECT
    # A fresh adapter after vendor's24h key window still calls the authoritative durable operation fence.
    later = ResendEmailAdapter(
        binding(),
        authority=fence,
        enabled=True,
        send_enabled=True,
        transport=httpx.MockTransport(handle),
        clock=FrozenClock(AT + timedelta(days=2)),
    )
    with pytest.raises(EmailFailure):
        await later.send_email(ctx(), command)
    assert count == 1


async def test_verified_webhook_retains_distinct_native_event_message_ids_and_replay_identity() -> None:
    from aco.integrations.ports.email_context import EmailWebhookContext

    raw = json.dumps(
        {
            "type": "email.received",
            "created_at": AT.isoformat(),
            "data": {
                "email_id": str(NATIVE),
                "to": ["inbox@example.test"],
                "from": "forged-author@example.test",
            },
        }
    ).encode()
    context = EmailWebhookContext(
        tenant_id=TENANT,
        actor_id=uuid4(),
        correlation_id=uuid4(),
        activation_id=uuid4(),
        email_connection_id=CONNECTION,
        raw_sha256=sha256(raw).hexdigest(),
    )
    secret = binding().webhook_secret
    assert secret is not None
    wh = Webhook(secret.get_secret_value())
    timestamp = datetime.now(UTC)
    signature = wh.sign("msg_offline1", timestamp, raw.decode())
    headers = {
        "svix-id": "msg_offline1",
        "svix-timestamp": str(int(timestamp.timestamp())),
        "svix-signature": signature,
    }
    port = adapter(lambda request: pytest.fail("Verification must not invoke HTTP"))
    first = await port.verify_webhook(context, raw, headers)
    replay = await port.verify_webhook(context, raw, headers)
    assert first.value == replay.value
    assert first.value[0].provider_ref.external_id == "msg_offline1"
    assert first.value[0].message_ref == reference() and first.value[0].kind == "CUSTOMER_REPLY"
    assert first.value[0].synthetic is False  # Provider delivery, never verified author authority.
    with pytest.raises(EmailFailure):
        await port.verify_webhook(context, raw + b" ", headers)
    old = timestamp - timedelta(hours=1)
    with pytest.raises(EmailFailure):
        await port.verify_webhook(
            context,
            raw,
            {
                "svix-id": "msg_offline1",
                "svix-timestamp": str(int(old.timestamp())),
                "svix-signature": wh.sign("msg_offline1", old, raw.decode()),
            },
        )


async def test_received_poll_has_scoped_cursor_and_never_claims_a_native_thread() -> None:
    from aco.integrations.ports.email_dto import EmailSyncCursor

    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/emails/receiving":
            assert request.url.params["to"] == "inbox@example.test"
            return httpx.Response(200, json={"data": [{"id": str(NATIVE)}], "has_more": True})
        if request.url.host == "api.resend.com":
            return httpx.Response(200, json=received())
        return httpx.Response(200, content=RAW)

    port = adapter(handle)
    page = await port.sync_messages(ctx())
    assert len(page.value.messages) == 1 and page.value.messages[0].provider_ref == reference()
    assert page.value.notifications == ()  # Poll results are not invented provider notifications.
    await port.sync_messages(ctx(), page.value.next_cursor)
    assert seen[3].url.params["after"] == str(NATIVE)
    with pytest.raises(EmailFailure):
        await port.sync_messages(
            ctx(),
            EmailSyncCursor(
                tenant_id=uuid4(), provider_connection_id=CONNECTION, token=page.value.next_cursor.token
            ),
        )


async def test_received_attachment_uses_typed_identity_and_expiry_refresh_without_exposing_urls() -> None:
    attachment_id = uuid4()
    reads = 0
    content = b"Synthetic PDF bytes"

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal reads
        if request.url.path == f"/emails/receiving/{NATIVE}":
            return httpx.Response(
                200,
                json=received(attachments=[{"id": str(attachment_id), "content_type": "application/pdf"}]),
            )
        if request.url.host == "api.resend.com":
            reads += 1
            assert request.url.path == f"/emails/receiving/{NATIVE}/attachments/{attachment_id}"
            return httpx.Response(
                200,
                json={
                    "id": str(attachment_id),
                    "filename": "invoice.pdf",
                    "content_type": "application/pdf",
                    "size": len(content),
                    "download_url": f"https://source.resend.test/attachment/{reads}",
                    "expires_at": (AT + timedelta(minutes=20)).isoformat(),
                },
            )
        assert "authorization" not in request.headers
        return httpx.Response(403 if reads == 1 else 200, content=content)

    ref = ProviderReference(
        provider="resend",
        connection_id=CONNECTION,
        external_id=f"received:{NATIVE}:attachment:{attachment_id}",
    )
    result = await adapter(handle).get_attachment(ctx(), ref)
    assert result.value == content and reads == 2


async def test_key_credentials_are_isolated_for_concurrent_tenant_clients() -> None:
    from dataclasses import replace

    second = replace(
        binding(), tenant_id=uuid4(), connection_id=uuid4(), api_key=SecretStr("re_other_fake_key")
    )
    observed = []

    async def handle(request: httpx.Request) -> httpx.Response:
        observed.append(request.headers["authorization"])
        await asyncio.sleep(0)
        return httpx.Response(200, json={"data": []})

    one = adapter(handle)
    two = ResendEmailAdapter(
        second, authority=authorized, enabled=True, transport=httpx.MockTransport(handle)
    )
    await asyncio.gather(one.validate_connection(ctx()), two.validate_connection(ctx(second.tenant_id)))
    assert sorted(observed) == ["Bearer re_other_fake_key", "Bearer re_synthetic_not_a_key"]


async def test_runtime_binding_is_explicit_default_off_and_checks_resolved_connection_identity() -> None:
    from aco.integrations.resend.runtime import ResendRuntime

    calls = []

    def resolve(tenant: UUID, connection: UUID) -> ResendBinding:
        calls.append((tenant, connection))
        return binding()

    disabled = ResendRuntime(resolve, authority=authorized)
    with pytest.raises(EmailFailure):
        disabled.bind(TENANT, CONNECTION)
    assert calls == []  # No implicit secret lookup or provider selection.
    active = ResendRuntime(
        resolve,
        authority=authorized,
        enabled=True,
        transport_factory=lambda: httpx.MockTransport(lambda request: httpx.Response(200, json={"data": []})),
    )
    port = active.bind(TENANT, CONNECTION)
    assert isinstance(port, EmailPort)
    assert (await port.validate_connection(ctx())).value.mode == "TRANSACTIONAL"
    with pytest.raises(EmailFailure):
        active.bind(uuid4(), CONNECTION)


async def test_unsupported_oauth_native_thread_subscription_and_provider_revocation_never_claim_success() -> (
    None
):
    from aco.integrations.ports.email_dto import MailboxAuthorization, NotificationSubscriptionRequest

    port = adapter(lambda request: pytest.fail("Unsupported operations make no HTTP request"))
    operations = [
        port.connect_authorized_mailbox(
            ctx(), MailboxAuthorization(operation_id=uuid4(), credential_reference="opaque-reference")
        ),
        port.fetch_thread(ctx(), reference()),
        port.subscribe_or_renew_notifications(ctx(), NotificationSubscriptionRequest(operation_id=uuid4())),
        port.disconnect(ctx()),
    ]
    for operation in operations:
        with pytest.raises(EmailFailure) as caught:
            await operation
        assert caught.value.failure.code == ProviderErrorCode.UNSUPPORTED_CAPABILITY
    features = (await port.describe_capabilities()).features
    assert not features.intersection(
        {"connect_authorized_mailbox", "fetch_thread", "subscribe_or_renew_notifications", "disconnect"}
    )


async def test_missing_native_raw_is_unavailable_not_synthesized_provider_original() -> None:
    port = adapter(lambda request: httpx.Response(200, json=received(raw=None)))
    item = (await port.get_message(ctx(), reference())).value
    assert item.source.kind == "UNAVAILABLE" and item.source.content is None
    assert item.message_id == "<reply@buyer.test>"


async def test_unknown_post_confirmation_and_untrusted_download_redirect_are_not_retried() -> None:
    attempts = []

    def malformed(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(200, json={"id": "not-a-native-id"})

    with pytest.raises(EmailFailure) as unknown:
        await adapter(malformed, send_enabled=True).send_email(ctx(), outgoing())
    assert unknown.value.failure.code == ProviderErrorCode.UNKNOWN_SIDE_EFFECT and len(attempts) == 1

    def redirect(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.resend.com":
            return httpx.Response(200, json=received())
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    with pytest.raises(EmailFailure):
        await adapter(redirect).get_message(ctx(), reference())


async def test_outbound_attachments_resolve_only_canonical_bytes_and_preserve_inline_cid() -> None:
    from aco.integrations.ports.email_dto import EmailAttachment, EmailObjectReference

    content = b"synthetic-image"
    item = EmailAttachment(
        filename="logo.png",
        content_type="image/png",
        disposition="inline",
        content_id="logo",
        size_bytes=len(content),
        integrity_sha256=sha256(content).hexdigest(),
        reference=EmailObjectReference(artifact_id=uuid4()),
    )
    command = outgoing().model_copy(update={"attachments": (item,)})

    async def resolve(context: object, attachment: object) -> bytes:
        assert attachment == item
        return content

    def handle(request: httpx.Request) -> httpx.Response:
        value = json.loads(request.content)["attachments"][0]
        assert value == {
            "filename": "logo.png",
            "content": base64.b64encode(content).decode(),
            "content_id": "logo",
        }
        assert "path" not in value
        return httpx.Response(200, json={"id": str(NATIVE)})

    await adapter(handle, send_enabled=True, attachment_resolver=resolve).send_email(ctx(), command)

    async def changed(context: object, attachment: object) -> bytes:
        return b"different"

    with pytest.raises(EmailFailure):
        await adapter(
            lambda request: pytest.fail("Changed bytes must fail before send"),
            send_enabled=True,
            attachment_resolver=changed,
        ).send_email(ctx(), command)


async def test_foreign_receiving_address_fails_before_private_original_download() -> None:
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=received(to=["other-tenant@example.test"]))

    with pytest.raises(EmailFailure):
        await adapter(handle).get_message(ctx(), reference())
    assert len(seen) == 1


async def test_source_over_limit_cannot_be_archived_as_complete() -> None:
    from dataclasses import replace

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.resend.com":
            return httpx.Response(200, json=received())
        return httpx.Response(200, content=RAW)

    port = ResendEmailAdapter(
        replace(binding(), max_source_bytes=8),
        authority=authorized,
        enabled=True,
        clock=FrozenClock(AT),
        transport=httpx.MockTransport(handle),
    )
    with pytest.raises(EmailFailure):
        await port.get_message(ctx(), reference())


async def test_transport_failure_does_not_expose_signed_url_or_provider_error_body_in_exception_chain() -> (
    None
):
    import traceback

    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("signed-url-secret-provider-detail", request=request)

    with pytest.raises(EmailFailure) as caught:
        await adapter(handle).get_message(ctx(), reference())
    assert "signed-url-secret-provider-detail" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("changes", [{"to": None}, {"headers": []}, {"attachments": [1]}])
async def test_malformed_provider_content_is_a_typed_no_effect_failure(changes: dict[str, Any]) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.resend.com":
            return httpx.Response(200, json=received(raw=None, **changes))
        pytest.fail("Invalid metadata must not start a raw download")

    with pytest.raises(EmailFailure) as caught:
        await adapter(handle).get_message(ctx(), reference())
    assert caught.value.failure.code == ProviderErrorCode.VALIDATION_FAILED


async def test_explicit_mocked_http_binding_never_presents_product_evidence_as_live_provider_proof() -> None:
    from dataclasses import replace

    config = replace(binding(), evidence_mode="MOCKED_HTTP")
    port = ResendEmailAdapter(
        config,
        authority=authorized,
        enabled=True,
        clock=FrozenClock(AT),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=received(raw=None))),
    )
    result = await port.get_message(ctx(), reference())
    assert (
        result.synthetic is True
        and result.native_identity == "SIMULATED"
        and result.authenticity == "SIMULATED"
    )
    assert result.value.synthetic is True and result.value.authenticity == "SIMULATED"


async def test_observation_fallback_is_explicit_application_identity_not_a_native_request_id() -> None:
    result = await adapter(lambda request: httpx.Response(200, json=received(raw=None))).get_message(
        ctx(), reference()
    )
    assert result.reference_origin == "APPLICATION" and result.provider_request_id is None
    assert result.value.provider_ref == reference()  # Actual native email identity stays separate.


async def test_wire_observer_finishes_metadata_raw_and_transport_failure_without_sensitive_fields() -> None:
    class Observer:
        def __init__(self) -> None:
            self.started_ops: list[str] = []
            self.results: list[tuple[object, object, object, object, object]] = []

        async def started(self, context: object, operation: str, material: object | None) -> object:
            self.started_ops.append(operation)
            return len(self.started_ops)

        async def finished(
            self, token: object, error: object, request_id: object, status: object, response_bytes: object
        ) -> None:
            self.results.append((token, error, request_id, status, response_bytes))

    observer = Observer()

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.resend.com":
            return httpx.Response(200, json=received(), headers={"x-request-id": "metadata-actual"})
        raise httpx.ReadTimeout("redacted-sensitive-url", request=request)

    port = adapter(handle, observer=observer)
    with pytest.raises(EmailFailure):
        await port.get_message(ctx(), reference())
    assert observer.started_ops == ["get_message", "download_original"]
    assert len(observer.results) == 2
    assert observer.results[0][2:4] == ("metadata-actual", 200)
    assert isinstance(observer.results[1][1], ProviderFailure)
    assert "redacted-sensitive-url" not in repr(observer.results)


async def test_observer_result_failure_after_send_is_unknown_and_never_resubmits() -> None:
    class Broken:
        async def started(self, context: object, operation: str, material: object | None) -> object:
            return uuid4()

        async def finished(
            self, token: object, error: object, request_id: object, status: object, response_bytes: object
        ) -> None:
            raise RuntimeError("synthetic evidence store unavailable")

    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": str(NATIVE)})

    with pytest.raises(EmailFailure) as caught:
        await adapter(handle, send_enabled=True, observer=Broken()).send_email(ctx(), outgoing())
    assert caught.value.failure.code == ProviderErrorCode.UNKNOWN_SIDE_EFFECT
    assert len(seen) == 1


async def test_observer_failure_after_read_retains_pending_evidence_without_automatic_retry_permission() -> (
    None
):
    class Broken:
        async def started(self, context: object, operation: str, material: object | None) -> object:
            return uuid4()

        async def finished(
            self, token: object, error: object, request_id: object, status: object, response_bytes: object
        ) -> None:
            raise RuntimeError("synthetic result persistence outage")

    with pytest.raises(EmailFailure) as caught:
        await adapter(
            lambda request: httpx.Response(200, json=received(raw=None)), observer=Broken()
        ).get_message(ctx(), reference())
    assert caught.value.failure.code == ProviderErrorCode.TEMPORARILY_UNAVAILABLE
    assert caught.value.failure.retryable is False


async def test_outbound_only_binding_does_not_claim_an_inbox_or_allow_received_reads() -> None:
    actual = ResendBinding(
        tenant_id=TENANT,
        connection_id=CONNECTION,
        api_key=SecretStr("offline-placeholder"),
        inbox_addresses=(),
        sender_addresses=("onboarding@resend.dev",),
        download_hosts=frozenset(),
    )
    wires: list[httpx.Request] = []

    def send(request: httpx.Request) -> httpx.Response:
        wires.append(request)
        assert request.method == "POST" and request.url.path == "/emails"
        return httpx.Response(200, json={"id": str(NATIVE)})

    port = ResendEmailAdapter(
        actual, authority=authorized, enabled=True, send_enabled=True, transport=httpx.MockTransport(send)
    )
    capabilities = await port.describe_capabilities()
    assert "send_email" in capabilities.features
    assert not {"sync_messages", "verify_webhook", "get_attachment"} & capabilities.features
    with pytest.raises(EmailFailure):
        await port.get_message(ctx(), reference())
    with pytest.raises(EmailFailure):
        await port.sync_messages(ctx(), None)
    with pytest.raises(EmailFailure):
        await port.send_email(ctx(), outgoing())
    assert wires == []
    accepted = await port.send_email(ctx(), outgoing().model_copy(update={"sender": "onboarding@resend.dev"}))
    assert accepted.status == "ACCEPTED" and len(wires) == 1
