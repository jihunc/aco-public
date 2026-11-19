"""Process-lifetime synthetic email contract; no runtime authorization or live provider proof."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from aco.core.context import ServiceContext, TenantContext
from aco.core.types import CostQuality, ProviderErrorCode, ProviderFailure, ProviderReference, Role

if TYPE_CHECKING:
    from aco.integrations.fake.email import FakeEmailAdapter
    from aco.integrations.ports.email_dto import EmailKind, EmailNotification, InboundEmail, OutboundEmail

TENANT = UUID("10000000-0000-0000-0000-000000000001")
CONNECTION = UUID("20000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 11, 18, 0, 0, tzinfo=UTC)


def context(tenant: UUID = TENANT) -> TenantContext:
    return TenantContext(tenant_id=tenant, actor_id=uuid4(), correlation_id=uuid4(), role=Role.OPERATOR)


# Override values deliberately include malformed inputs at this Pydantic validation boundary.
def command(**changes: Any) -> OutboundEmail:
    from aco.integrations.ports.email_dto import OutboundEmail

    return OutboundEmail(
        **{
            "tenant_id": TENANT,
            "provider_connection_id": CONNECTION,
            "sender": "collections@example.test",
            "to": ("buyer@example.test",),
            "cc": ("finance@example.test",),
            "bcc": ("archive@example.test",),
            "subject": "Invoice INV-1",
            "text_body": "Please review INV-1.",
            "html_body": "<p>INV-1</p>",
            "origin_invoice_id": uuid4(),
            "conversation_thread_id": uuid4(),
            "operation_id": uuid4(),
            "message_id": "<aco-1@example.test>",
            "in_reply_to": "<prior@example.test>",
            "references": ("<first@example.test>", "<prior@example.test>"),
            **changes,
        }
    )


def message(**changes: Any) -> InboundEmail:
    from aco.integrations.ports.email_dto import EmailSource, InboundEmail

    raw = b"From: buyer@example.test\r\nMessage-ID: <reply@example.test>\r\n\r\nI will pay Friday."
    return InboundEmail(
        **{
            "tenant_id": TENANT,
            "provider_connection_id": CONNECTION,
            "provider_ref": ProviderReference(
                provider="fake_email", connection_id=CONNECTION, external_id="simulated-message:reply"
            ),
            "native_identity": "SIMULATED",
            "synthetic": True,
            "sender": "buyer@example.test",
            "to": ("collections@example.test",),
            "subject": "Re: Invoice INV-1",
            "text_body": "I will pay Friday.",
            "headers": (("message-id", "<reply@example.test>"), ("x-trace", "one"), ("x-trace", "two")),
            "message_id": "<reply@example.test>",
            "in_reply_to": "<aco-1@example.test>",
            "references": ("<aco-1@example.test>",),
            "received_at": NOW,
            "provider_thread_ref": ProviderReference(
                provider="fake_email", connection_id=CONNECTION, external_id="simulated-thread:1"
            ),
            "source": EmailSource(
                kind="PROVIDER_ORIGINAL", content=raw, integrity_sha256=sha256(raw).hexdigest()
            ),
            **changes,
        }
    )


def adapter(
    *,
    messages: tuple[InboundEmail, ...] = (),
    notifications: tuple[EmailNotification, ...] = (),
    send_failure: ProviderFailure | None = None,
) -> FakeEmailAdapter:
    from aco.integrations.fake.email import FakeEmailAdapter

    return FakeEmailAdapter(
        tenant_id=TENANT,
        connection_id=CONNECTION,
        messages=messages,
        notifications=notifications,
        send_failure=send_failure,
    )


async def test_send_preserves_payload_and_composed_source_without_claiming_delivery() -> None:
    """Losing RFC references, envelope recipients or confusing composed bytes must fail."""
    from aco.integrations.ports.email_dto import EmailAttachment, EmailObjectReference

    attachment = EmailAttachment(
        filename="invoice.pdf",
        content_type="application/pdf",
        reference=EmailObjectReference(artifact_id=uuid4()),
    )
    request = command(attachments=(attachment,))
    fake, ctx = adapter(), context()
    attempt = await fake.send_email(ctx, request)
    assert attempt.status == "ACCEPTED"
    assert attempt.operation_id == request.operation_id
    assert attempt.cost.quality == CostQuality.UNKNOWN and attempt.cost.amount is None
    assert attempt.synthetic and attempt.native_identity == "SIMULATED"
    assert attempt.tenant_id == TENANT and attempt.provider_connection_id == CONNECTION
    assert attempt.correlation_id == ctx.correlation_id and attempt.attempt == 1
    assert attempt.provider_ref.external_id.startswith("simulated-request:")
    assert attempt.revision and attempt.observed_at.tzinfo
    saved = (await fake.get_message(ctx, attempt.message_ref)).value
    assert saved.direction == "outgoing" and saved.kind == "OUTBOUND_ECHO"
    assert (saved.sender, saved.to, saved.cc, saved.bcc, saved.subject, saved.text_body, saved.html_body) == (
        request.sender,
        request.to,
        request.cc,
        request.bcc,
        request.subject,
        request.text_body,
        request.html_body,
    )
    assert saved.attachments == (attachment,)
    assert (saved.message_id, saved.in_reply_to, saved.references) == (
        "<aco-1@example.test>",
        "<prior@example.test>",
        ("<first@example.test>", "<prior@example.test>"),
    )
    assert saved.origin_invoice_id == request.origin_invoice_id
    assert saved.conversation_thread_id == request.conversation_thread_id
    assert saved.source.kind == "ACO_COMPOSED"
    assert saved.source.content is not None
    assert sha256(saved.source.content).hexdigest() == saved.source.integrity_sha256
    assert b"Invoice INV-1" in saved.source.content


async def test_identical_operation_returns_one_message_and_same_native_attempt() -> None:
    fake, ctx, request = adapter(), context(), command()
    first = await fake.send_email(ctx, request)
    again = await fake.send_email(ctx, request)
    assert again == first
    page = (await fake.sync_messages(ctx)).value
    assert len(page.messages) == 1
    assert page.messages[0].provider_ref == first.message_ref


@pytest.mark.parametrize(
    "field,value",
    [
        ("subject", "Changed"),
        ("to", ("other@example.test",)),
        ("text_body", "Changed"),
        ("origin_invoice_id", UUID(int=91)),
        ("conversation_thread_id", UUID(int=92)),
        ("references", ("<changed@example.test>",)),
        ("provider_connection_id", UUID(int=93)),
        ("tenant_id", UUID(int=94)),
    ],
)
async def test_operation_identity_cannot_reuse_changed_payload_or_scope(field: str, value: object) -> None:
    from aco.integrations.ports.email import EmailFailure

    fake, ctx, request = adapter(), context(), command()
    first = await fake.send_email(ctx, request)
    with pytest.raises(EmailFailure) as caught:
        await fake.send_email(ctx, command(**{**request.model_dump(), field: value}))
    assert caught.value.failure.code == ProviderErrorCode.VALIDATION_FAILED
    assert not caught.value.failure.may_have_side_effect
    assert len((await fake.sync_messages(ctx)).value.messages) == 1
    assert (await fake.get_message(ctx, first.message_ref)).value.subject == "Invoice INV-1"


async def test_unknown_effect_is_retained_and_never_blindly_resubmitted() -> None:
    from aco.integrations.ports.email import EmailFailure

    unknown = ProviderFailure(
        code=ProviderErrorCode.UNKNOWN_SIDE_EFFECT,
        retryable=False,
        may_have_side_effect=True,
        request_id="simulated-lost-response",
    )
    fake, ctx, request = adapter(send_failure=unknown), context(), command()
    for _ in range(2):
        with pytest.raises(EmailFailure) as caught:
            await fake.send_email(ctx, request)
        assert caught.value.failure == unknown
    page = (await fake.sync_messages(ctx)).value
    assert len(page.messages) == 1
    assert page.messages[0].operation_id == request.operation_id


@pytest.mark.parametrize(
    "code,retryable",
    [
        (ProviderErrorCode.AUTH_EXPIRED, False),
        (ProviderErrorCode.RATE_LIMITED, True),
        (ProviderErrorCode.TEMPORARILY_UNAVAILABLE, True),
        (ProviderErrorCode.VALIDATION_FAILED, False),
    ],
)
async def test_normalized_send_errors_preserve_metadata_without_invented_effect(
    code: ProviderErrorCode, retryable: bool
) -> None:
    from aco.integrations.ports.email import EmailFailure

    failure = ProviderFailure(
        code=code,
        retryable=retryable,
        may_have_side_effect=False,
        request_id="simulated-error",
        retry_after=timedelta(seconds=7) if code == ProviderErrorCode.RATE_LIMITED else None,
    )
    fake, ctx = adapter(send_failure=failure), context()
    with pytest.raises(EmailFailure) as caught:
        await fake.send_email(ctx, command())
    assert caught.value.failure == failure
    assert (await fake.sync_messages(ctx)).value.messages == ()


async def test_inbound_raw_headers_attachments_and_thread_survive_fetch() -> None:
    from aco.integrations.ports.email_dto import EmailAttachment, EmailObjectReference

    item = message(
        attachments=(
            EmailAttachment(
                filename="detail.txt",
                content_type="text/plain",
                reference=EmailObjectReference(
                    provider_ref=ProviderReference(
                        provider="fake_email", connection_id=CONNECTION, external_id="simulated-attachment:1"
                    )
                ),
            ),
        )
    )
    fake, ctx = adapter(messages=(item,)), context()
    fetched = (await fake.get_message(ctx, item.provider_ref)).value
    assert fetched == item
    assert item.provider_thread_ref is not None
    thread = (await fake.fetch_thread(ctx, item.provider_thread_ref)).value
    assert thread == (item,)
    assert fetched.headers == (("message-id", "<reply@example.test>"), ("x-trace", "one"), ("x-trace", "two"))
    assert fetched.received_at == NOW
    assert fetched.source.kind == "PROVIDER_ORIGINAL"
    assert fetched.source.content is not None
    assert fetched.source.content.endswith(b"I will pay Friday.")
    assert fetched.source.integrity_sha256 == sha256(fetched.source.content).hexdigest()


async def test_distinct_notifications_do_not_duplicate_one_native_message() -> None:
    from aco.integrations.ports.email_dto import EmailNotification

    item = message()
    events = tuple(
        EmailNotification(
            tenant_id=TENANT,
            provider_connection_id=CONNECTION,
            provider_ref=ProviderReference(
                provider="fake_email", connection_id=CONNECTION, external_id=event_id
            ),
            message_ref=item.provider_ref,
            kind="CUSTOMER_REPLY",
            occurred_at=NOW,
            synthetic=True,
            native_identity="SIMULATED",
        )
        for event_id in ("simulated-event:1", "simulated-event:2")
    )
    fake, ctx = adapter(messages=(item, item), notifications=(*events, events[0])), context()
    page = (await fake.sync_messages(ctx)).value
    assert len(page.messages) == 1
    assert tuple(event.provider_ref.external_id for event in page.notifications) == (
        "simulated-event:1",
        "simulated-event:2",
    )
    assert page.notifications[0].message_ref == page.notifications[1].message_ref
    next_page = (await fake.sync_messages(ctx, page.next_cursor)).value
    assert next_page.messages == () and next_page.notifications == ()


@pytest.mark.parametrize("kind", ["DELIVERY_RECEIPT", "BOUNCE", "OUTBOUND_ECHO"])
async def test_delivery_notifications_never_become_customer_reply(kind: EmailKind) -> None:
    from aco.integrations.ports.email_dto import EmailNotification

    item = message()
    event = EmailNotification(
        tenant_id=TENANT,
        provider_connection_id=CONNECTION,
        provider_ref=ProviderReference(
            provider="fake_email", connection_id=CONNECTION, external_id="simulated-event:receipt"
        ),
        message_ref=item.provider_ref,
        kind=kind,
        occurred_at=NOW,
        synthetic=True,
        native_identity="SIMULATED",
    )
    page = (await adapter(messages=(item,), notifications=(event,)).sync_messages(context())).value
    assert page.notifications[0].kind == kind
    assert page.notifications[0].kind != "CUSTOMER_REPLY"


@pytest.mark.parametrize(
    "method,args",
    [
        ("validate_connection", ()),
        ("disconnect", ()),
        ("sync_messages", ()),
        (
            "get_message",
            (
                ProviderReference(
                    provider="fake_email", connection_id=CONNECTION, external_id="simulated-message:reply"
                ),
            ),
        ),
        (
            "fetch_thread",
            (
                ProviderReference(
                    provider="fake_email", connection_id=CONNECTION, external_id="simulated-thread:1"
                ),
            ),
        ),
    ],
)
async def test_cross_tenant_context_rejected_for_each_scoped_operation(
    method: str, args: tuple[ProviderReference, ...]
) -> None:
    from aco.integrations.ports.email import EmailFailure

    fake = adapter(messages=(message(),))
    with pytest.raises(EmailFailure) as caught:
        await getattr(fake, method)(context(uuid4()), *args)
    assert caught.value.failure.code == ProviderErrorCode.VALIDATION_FAILED
    assert (await fake.validate_connection(context())).value.connected


async def test_send_rejects_foreign_context_before_any_effect() -> None:
    from aco.integrations.ports.email import EmailFailure

    fake = adapter()
    with pytest.raises(EmailFailure):
        await fake.send_email(context(uuid4()), command())
    assert (await fake.sync_messages(context())).value.messages == ()


@pytest.mark.parametrize(
    "foreign",
    [
        ProviderReference(provider="other", connection_id=CONNECTION, external_id="simulated-message:reply"),
        ProviderReference(
            provider="fake_email", connection_id=UUID(int=99), external_id="simulated-message:reply"
        ),
        ProviderReference(provider="fake_email", connection_id=CONNECTION, external_id="unknown"),
    ],
)
async def test_foreign_or_unknown_message_reference_is_rejected(foreign: ProviderReference) -> None:
    from aco.integrations.ports.email import EmailFailure

    with pytest.raises(EmailFailure) as caught:
        await adapter(messages=(message(),)).get_message(context(), foreign)
    assert caught.value.failure.code == ProviderErrorCode.VALIDATION_FAILED


async def test_cursor_cannot_cross_tenant_connection_or_adapter_instance() -> None:
    from aco.integrations.ports.email import EmailFailure

    fake, ctx = adapter(), context()
    cursor = (await fake.sync_messages(ctx)).value.next_cursor
    for invalid in (
        cursor.model_copy(update={"tenant_id": uuid4()}),
        cursor.model_copy(update={"provider_connection_id": uuid4()}),
        cursor.model_copy(update={"token": "invented"}),
    ):
        with pytest.raises(EmailFailure):
            await fake.sync_messages(ctx, invalid)
    with pytest.raises(EmailFailure):
        await adapter().sync_messages(ctx, cursor)


async def test_service_context_is_accepted_as_value_scope_without_claiming_authentication() -> None:
    ctx = ServiceContext(
        tenant_id=TENANT, actor_id=uuid4(), correlation_id=uuid4(), action_id=uuid4(), action_version=1
    )
    result = await adapter().send_email(ctx, command())
    assert result.correlation_id == ctx.correlation_id


async def test_disconnect_revokes_send_read_sync_and_connection_validation_reports_disconnected() -> None:
    from aco.integrations.ports.email import EmailFailure

    fake, ctx = adapter(messages=(message(),)), context()
    assert (await fake.disconnect(ctx)).value.connected is False
    assert (await fake.disconnect(ctx)).value.connected is False
    assert (await fake.validate_connection(ctx)).value.connected is False
    for method, args in [
        ("send_email", (command(),)),
        ("get_message", (message().provider_ref,)),
        ("fetch_thread", (message().provider_thread_ref,)),
        ("sync_messages", ()),
    ]:
        with pytest.raises(EmailFailure) as caught:
            await getattr(fake, method)(ctx, *args)
        assert caught.value.failure.code == ProviderErrorCode.AUTH_EXPIRED


async def test_all_ten_methods_and_capabilities_preserve_explicit_unsupported_operations() -> None:
    from aco.integrations.ports.email import EmailFailure, EmailPort
    from aco.integrations.ports.email_dto import MailboxAuthorization, NotificationSubscriptionRequest

    fake, ctx = adapter(), context()
    assert isinstance(fake, EmailPort)
    capabilities = await fake.describe_capabilities()
    assert capabilities.features == frozenset(
        {
            "send_email",
            "get_message",
            "fetch_thread",
            "sync_messages",
            "validate_connection",
            "disconnect",
            "synthetic",
            "process_lifetime_idempotency",
        }
    )
    for method, args in [
        (
            "connect_authorized_mailbox",
            (MailboxAuthorization(operation_id=uuid4(), credential_reference="opaque-secret-reference"),),
        ),
        ("subscribe_or_renew_notifications", (NotificationSubscriptionRequest(operation_id=uuid4()),)),
        ("verify_webhook", (b"{}", {"signature": "not-real"})),
    ]:
        with pytest.raises(EmailFailure) as caught:
            await getattr(fake, method)(ctx, *args)
        assert caught.value.failure.code == ProviderErrorCode.UNSUPPORTED_CAPABILITY
        assert not caught.value.failure.retryable and not caught.value.failure.may_have_side_effect


@pytest.mark.parametrize(
    "source",
    [
        {"kind": "PROVIDER_ORIGINAL", "content": b"raw", "integrity_sha256": "0" * 64},
        {"kind": "PROVIDER_ORIGINAL"},
        {"kind": "UNAVAILABLE", "content": b"invented"},
        {"kind": "UNAVAILABLE", "integrity_sha256": "0" * 64},
        {"kind": "ACO_COMPOSED", "content": b"composed"},
    ],
)
def test_source_evidence_rejects_hash_mismatch_fabrication_and_false_completeness(
    source: dict[str, Any],
) -> None:
    from aco.integrations.ports.email_dto import EmailSource

    with pytest.raises(ValidationError):
        EmailSource(**source)


def test_unavailable_source_and_provider_pointer_remain_explicitly_distinct() -> None:
    from aco.integrations.ports.email_dto import EmailObjectReference, EmailSource

    missing = message(source=EmailSource(kind="UNAVAILABLE"))
    assert missing.source.content is None and missing.source.integrity_sha256 is None
    pointer = EmailSource(
        kind="PROVIDER_ORIGINAL",
        reference=EmailObjectReference(
            provider_ref=ProviderReference(
                provider="fake_email", connection_id=CONNECTION, external_id="simulated-object:raw"
            )
        ),
    )
    assert pointer.content is None and pointer.integrity_sha256 is None
    assert pointer.kind == "PROVIDER_ORIGINAL"


def test_inbound_cannot_claim_an_aco_composed_representation_is_provider_original() -> None:
    from aco.integrations.ports.email_dto import EmailSource

    raw = b"ACO synthesized this"
    with pytest.raises(ValidationError):
        message(
            source=EmailSource(kind="ACO_COMPOSED", content=raw, integrity_sha256=sha256(raw).hexdigest())
        )


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {
            "artifact_id": uuid4(),
            "provider_ref": ProviderReference(
                provider="fake_email", connection_id=CONNECTION, external_id="simulated-object:1"
            ),
        },
        {"url": "https://unsafe.example/raw"},
    ],
)
def test_attachment_reference_requires_one_typed_object_identity_not_a_fetch_url(
    fields: dict[str, Any],
) -> None:
    from aco.integrations.ports.email_dto import EmailObjectReference

    with pytest.raises(ValidationError):
        EmailObjectReference(**fields)


@pytest.mark.parametrize(
    "changes",
    [
        {"to": ()},
        {"subject": "Bad\r\nBcc: injected@example.test"},
        {"sender": "a@example.test\r\nBad: header"},
        {"to": ("",)},
        {"message_id": "<bad>\n"},
        {"received_at": datetime(2026, 11, 18)},
    ],
)
def test_canonical_dto_rejects_unsafe_headers_empty_recipients_and_naive_time(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        message(**changes) if "received_at" in changes else command(**changes)


async def test_attachment_and_native_thread_reference_connection_must_match_bound_adapter() -> None:
    from aco.integrations.ports.email import EmailFailure
    from aco.integrations.ports.email_dto import EmailAttachment, EmailObjectReference

    foreign = ProviderReference(
        provider="fake_email", connection_id=uuid4(), external_id="simulated-object:1"
    )
    for changes in (
        {"provider_thread_ref": foreign},
        {
            "attachments": (
                EmailAttachment(
                    filename="other.pdf",
                    content_type="application/pdf",
                    reference=EmailObjectReference(provider_ref=foreign),
                ),
            )
        },
    ):
        fake = adapter()
        with pytest.raises(EmailFailure):
            await fake.send_email(context(), command(**changes))
        assert (await fake.sync_messages(context())).value.messages == ()


def test_seed_cannot_import_foreign_or_conflicting_native_identity() -> None:
    from aco.integrations.ports.email import EmailFailure

    for messages in (
        (message(tenant_id=uuid4()),),
        (message(), message(text_body="Conflicting body")),
        (message(synthetic=False, native_identity="PROVIDER", authenticity="VERIFIED"),),
    ):
        with pytest.raises(EmailFailure):
            adapter(messages=messages)


async def test_even_known_unsent_operation_cannot_silently_change_its_bound_payload() -> None:
    from aco.integrations.ports.email import EmailFailure

    rate_limited = ProviderFailure(
        code=ProviderErrorCode.RATE_LIMITED,
        retryable=True,
        may_have_side_effect=False,
        request_id="simulated-rate-limit",
    )
    fake, ctx, request = adapter(send_failure=rate_limited), context(), command()
    with pytest.raises(EmailFailure) as original:
        await fake.send_email(ctx, request)
    assert original.value.failure.code == ProviderErrorCode.RATE_LIMITED
    with pytest.raises(EmailFailure) as changed:
        await fake.send_email(ctx, command(**{**request.model_dump(), "subject": "Different intent"}))
    assert changed.value.failure.code == ProviderErrorCode.VALIDATION_FAILED
    assert (await fake.sync_messages(ctx)).value.messages == ()


async def test_composed_source_retains_generated_rfc_and_native_thread_identity() -> None:
    import json

    fake, ctx = adapter(), context()
    attempt = await fake.send_email(ctx, command(message_id=None))
    saved = (await fake.get_message(ctx, attempt.message_ref)).value
    assert saved.source.content is not None
    representation = json.loads(saved.source.content)
    assert representation["message_id"] == saved.message_id
    assert saved.message_id is not None
    assert saved.message_id.startswith("<simulated-")
    assert saved.provider_thread_ref is not None
    assert representation["provider_thread_ref"]["external_id"] == saved.provider_thread_ref.external_id
    assert saved.source.content_type == "application/vnd.aco.outbound-email+json"
    assert ("in-reply-to", "<prior@example.test>") in saved.headers
    assert ("references", "<first@example.test> <prior@example.test>") in saved.headers


def test_provider_timestamp_is_canonical_utc_without_changing_the_instant() -> None:
    from datetime import timezone

    received = datetime(2026, 11, 18, 11, 0, tzinfo=timezone(timedelta(hours=11)))
    item = message(received_at=received)
    assert item.received_at == NOW
    assert item.received_at.utcoffset() == timedelta(0)


def test_consumer_supplied_composed_rfc822_preserves_real_bytes_and_attachment_content() -> None:
    """Characterize the public DTO seam; fake normalized capture is not original MIME."""
    from email import policy
    from email.message import EmailMessage as RfcEmail
    from email.parser import BytesParser

    from aco.integrations.ports.email_dto import EmailMessage, EmailSource

    mime = RfcEmail(policy=policy.SMTP)
    mime["From"] = "collections@example.test"
    mime["To"] = "buyer@example.test"
    mime["Message-ID"] = "<composed@example.test>"
    mime.set_content("Please review INV-1.")
    mime.add_attachment(
        b"actual supplied attachment", maintype="application", subtype="pdf", filename="invoice.pdf"
    )
    raw = mime.as_bytes()
    supplied = EmailMessage(
        **{
            **message().model_dump(),
            "direction": "outgoing",
            "kind": "OUTBOUND_ECHO",
            "received_at": None,
            "sent_at": NOW,
            "source": EmailSource(
                kind="ACO_COMPOSED",
                content=raw,
                integrity_sha256=sha256(raw).hexdigest(),
                content_type="message/rfc822",
            ),
        }
    )
    captured = EmailMessage.model_validate_json(supplied.model_dump_json())
    assert captured.source.kind == "ACO_COMPOSED"
    assert captured.source.content_type == "message/rfc822"
    assert captured.source.content == raw
    parsed = BytesParser(policy=policy.default).parsebytes(captured.source.content)
    assert parsed["Message-ID"] == "<composed@example.test>"
    assert next(parsed.iter_attachments()).get_payload(decode=True) == b"actual supplied attachment"


def test_original_mime_binary_octets_survive_dto_serialization_without_text_decoding() -> None:
    from aco.integrations.ports.email_dto import InboundEmail

    raw = b"Content-Type: application/octet-stream\r\n\r\n\xff\x00\x80exact original"
    from aco.integrations.ports.email_dto import EmailSource

    original = message(
        source=EmailSource(kind="PROVIDER_ORIGINAL", content=raw, integrity_sha256=sha256(raw).hexdigest())
    )
    restored = InboundEmail.model_validate_json(original.model_dump_json())
    assert restored.source.content == raw
    assert restored.source.integrity_sha256 == sha256(raw).hexdigest()


@pytest.mark.parametrize(
    "captured",
    [
        {"sender": None, "to": ()},
        {"sender": "Unverified display text <not a mailbox>", "to": ("malformed destination",)},
        {"subject": "Untrusted subject\r\nFrom: forged@example.test"},
        {
            "message_id": "not a valid RFC identifier",
            "in_reply_to": "unverified reference",
            "references": ("unparseable reference",),
        },
        {"headers": (("X-Untrusted", "original\r\n malformed continuation"),)},
        {"text_body": None, "html_body": None},
    ],
)
async def test_untrusted_inbound_capture_retains_evidence_without_granting_author_authority(
    captured: dict[str, Any],
) -> None:
    from aco.integrations.ports.email_dto import InboundEmail

    original = message(**captured, authenticity="VERIFIED")
    saved = (await adapter(messages=(original,)).get_message(context(), original.provider_ref)).value
    assert saved.source.content == original.source.content
    assert saved.provider_ref.external_id == "simulated-message:reply"
    assert saved.received_at == NOW
    restored = InboundEmail.model_validate_json(saved.model_dump_json())
    for field, value in captured.items():
        expected = tuple((name.lower(), item) for name, item in value) if field == "headers" else value
        assert getattr(restored, field) == expected
    assert saved.authenticity == "VERIFIED"  # Delivery/source verification is not author authority.


@pytest.mark.parametrize(
    "changes",
    [
        {"sender": None},
        {"to": ()},
        {"to": ("malformed destination",)},
        {"message_id": "not a valid RFC identifier"},
        {"references": ("unparseable reference",)},
        {"text_body": None, "html_body": None},
    ],
)
def test_outbound_validation_remains_strict_when_inbound_capture_is_untrusted(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        command(**changes)


@pytest.mark.parametrize(
    "destination", ["one,two@example.test", "local:part@example.test", "address..dots@example.test"]
)
def test_outbound_canonical_mailbox_rejects_invalid_rfc_local_part(destination: str) -> None:
    with pytest.raises(ValidationError):
        command(to=(destination,))
