"""Per-connection HTTPX Resend adapter; actual API shapes, no SDK global credential state.

Authority and permanent operation fencing belong to the injected canonical application
validator. Constructing this adapter, holding a key, or vendor idempotency is not authority.
"""

import base64
import json
import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from email import policy
from email.parser import BytesParser
from hashlib import sha256
from typing import Any, Protocol
from uuid import UUID, uuid4

import httpx
from pydantic import ValidationError

from aco.core.clock import Clock, SystemClock
from aco.core.context import ActorContext, ServiceContext, TenantContext
from aco.core.types import (
    CostQuality,
    ProviderCapabilities,
    ProviderErrorCode,
    ProviderFailure,
    ProviderReference,
)
from aco.integrations.ports.email import EmailFailure
from aco.integrations.ports.email_context import EmailIngressContext, EmailWebhookContext
from aco.integrations.ports.email_dto import (
    DeliveryAttempt,
    EmailAttachment,
    EmailConnectionState,
    EmailMessage,
    EmailNotification,
    EmailObjectReference,
    EmailResult,
    EmailSource,
    EmailSubscription,
    EmailSyncCursor,
    EmailSyncPage,
    MailboxAuthorization,
    NotificationSubscriptionRequest,
    OutboundEmail,
)
from aco.integrations.ports.email_observer import EmailRequestObserver
from aco.integrations.resend.payloads import EmailEnvelope
from aco.integrations.resend.settings import ResendBinding

EmailContext = ActorContext | EmailIngressContext | EmailWebhookContext


class ResendAuthority(Protocol):
    async def __call__(self, ctx: EmailContext, operation: str, material: object | None, /) -> None:
        """Freshly validate persisted tenant/connection/current authority and exact material.

        send_email MUST additionally fence the permanent canonical operation before any
        provider invocation. UNKNOWN/accepted operations never regain submission authority
        when Resend's24h idempotency key expires. Adapter retries are disabled.
        """
        ...


class AttachmentResolver(Protocol):
    async def __call__(self, ctx: ActorContext, attachment: EmailAttachment, /) -> bytes: ...


def failure(
    code: ProviderErrorCode = ProviderErrorCode.VALIDATION_FAILED,
    *,
    request_id: str | None = None,
    retry_after: timedelta | None = None,
) -> EmailFailure:
    return EmailFailure(
        ProviderFailure(
            code=code,
            retryable=code
            in {
                ProviderErrorCode.RATE_LIMITED,
                ProviderErrorCode.TEMPORARILY_UNAVAILABLE,
            },
            may_have_side_effect=code == ProviderErrorCode.UNKNOWN_SIDE_EFFECT,
            request_id=request_id,
            retry_after=retry_after,
        )
    )


def safe_id(value: object) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", value) else None


class ResendEmailAdapter:
    provider = "resend"

    def __init__(
        self,
        binding: ResendBinding,
        *,
        authority: ResendAuthority,
        enabled: bool = False,
        send_enabled: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock | None = None,
        attachment_resolver: AttachmentResolver | None = None,
        observer: EmailRequestObserver | None = None,
    ):
        if binding.evidence_mode == "MOCKED_HTTP" and transport is None:
            raise failure(ProviderErrorCode.VALIDATION_FAILED)
        self.binding, self.authority = binding, authority
        self.observer = observer
        self.tenant_id, self.connection_id = binding.tenant_id, binding.connection_id
        self.enabled, self.send_enabled = enabled, send_enabled
        self.transport, self.clock, self.attachment_resolver = (
            transport,
            clock or SystemClock(),
            attachment_resolver,
        )
        self._connected = True

    async def _gate(self, ctx: EmailContext, operation: str, material: object | None = None) -> None:
        if not isinstance(ctx, TenantContext | ServiceContext | EmailIngressContext | EmailWebhookContext):
            raise failure()
        if not self.enabled or not self._connected or ctx.tenant_id != self.tenant_id:
            raise failure(ProviderErrorCode.AUTH_EXPIRED)
        if (
            isinstance(ctx, EmailIngressContext | EmailWebhookContext)
            and ctx.email_connection_id != self.connection_id
        ):
            raise failure()
        if not self.binding.inbox_addresses and (
            operation in {"sync_messages", "verify_webhook", "get_attachment", "download_original"}
            or (isinstance(material, ProviderReference) and material.external_id.startswith("received:"))
        ):
            raise failure(ProviderErrorCode.UNSUPPORTED_CAPABILITY)
        await self.authority(ctx, operation, material)

    def _ref(self, value: str) -> ProviderReference:
        return ProviderReference(provider=self.provider, connection_id=self.connection_id, external_id=value)

    def _native(self, reference: ProviderReference) -> tuple[str, UUID]:
        if (reference.provider, reference.connection_id) != (self.provider, self.connection_id):
            raise failure()
        try:
            direction, native = reference.external_id.split(":")
            if direction not in {"received", "sent"}:
                raise ValueError()
            return direction, UUID(native)
        except ValueError:
            raise failure() from None

    def _observation(self, ctx: EmailContext, request_id: str | None = None) -> dict[str, Any]:
        from aco.core.types import ProviderCost

        return dict(
            tenant_id=self.tenant_id,
            provider_connection_id=self.connection_id,
            provider_ref=self._ref(request_id or f"aco-observation:{uuid4()}"),
            reference_origin="PROVIDER" if request_id else "APPLICATION",
            provider_request_id=request_id,
            correlation_id=ctx.correlation_id,
            observed_at=self.clock.now(),
            revision=None,
            native_identity="SIMULATED" if self.binding.evidence_mode == "MOCKED_HTTP" else "PROVIDER",
            synthetic=self.binding.evidence_mode == "MOCKED_HTTP",
            authenticity="SIMULATED" if self.binding.evidence_mode == "MOCKED_HTTP" else "VERIFIED",
            attempt=1,
            cost=ProviderCost(
                amount=None, currency=self.binding.billing_currency, quality=CostQuality.UNKNOWN
            ),
        )

    async def _request(
        self,
        ctx: EmailContext,
        method: str,
        path: str,
        *,
        material: object | None = None,
        operation: str,
        payload: dict[str, Any] | None = None,
        key: str | None = None,
        params: dict[str, str] | None = None,
        download: bool = False,
    ) -> httpx.Response:
        await self._gate(ctx, operation, material)
        token = await self.observer.started(ctx, operation, material) if self.observer else None
        trace: dict[str, Any] = {"status": None, "request_id": None, "bytes": 0}
        try:
            response = await self._wire(
                ctx,
                method,
                path,
                material=material,
                operation=operation,
                payload=payload,
                key=key,
                params=params,
                download=download,
                trace=trace,
            )
        except EmailFailure as error:
            await self._finish_observation(token, error.failure, trace, method)
            raise
        except Exception:
            unexpected = failure(
                ProviderErrorCode.UNKNOWN_SIDE_EFFECT
                if method == "POST"
                else ProviderErrorCode.TEMPORARILY_UNAVAILABLE
            )
            await self._finish_observation(token, unexpected.failure, trace, method)
            raise unexpected from None
        observed_failure = (
            failure(ProviderErrorCode.TEMPORARILY_UNAVAILABLE).failure
            if download and response.status_code in {401, 403, 404}
            else None
        )
        await self._finish_observation(token, observed_failure, trace, method)
        return response

    async def _finish_observation(
        self,
        token: object,
        provider_failure: ProviderFailure | None,
        trace: dict[str, Any],
        method: str,
    ) -> None:
        if self.observer is None:
            return
        try:
            await self.observer.finished(
                token, provider_failure, trace["request_id"], trace["status"], trace["bytes"]
            )
        except Exception:
            raise EmailFailure(
                ProviderFailure(
                    code=ProviderErrorCode.UNKNOWN_SIDE_EFFECT
                    if method == "POST"
                    else ProviderErrorCode.TEMPORARILY_UNAVAILABLE,
                    retryable=False,
                    may_have_side_effect=method == "POST",
                )
            ) from None

    async def _wire(
        self,
        ctx: EmailContext,
        method: str,
        path: str,
        *,
        material: object | None = None,
        operation: str,
        payload: dict[str, Any] | None = None,
        key: str | None = None,
        params: dict[str, str] | None = None,
        download: bool = False,
        trace: dict[str, Any],
    ) -> httpx.Response:
        url = path if download else "https://api.resend.com" + path
        headers = {} if download else {"Authorization": "Bearer " + self.binding.api_key.get_secret_value()}
        if key:
            headers["Idempotency-Key"] = key
        cap = self.binding.max_source_bytes if download else 2 * 1024 * 1024
        try:
            async with httpx.AsyncClient(
                transport=self.transport, trust_env=False, follow_redirects=False, timeout=httpx.Timeout(15.0)
            ) as client:
                async with client.stream(
                    method, url, headers=headers, json=payload, params=params
                ) as response:
                    trace["status"] = response.status_code
                    trace["request_id"] = safe_id(response.headers.get("x-request-id"))
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        trace["bytes"] = size
                        if size > cap:
                            raise failure(
                                ProviderErrorCode.UNKNOWN_SIDE_EFFECT
                                if method == "POST"
                                else ProviderErrorCode.VALIDATION_FAILED
                            )
                        chunks.append(chunk)
                    result = httpx.Response(
                        response.status_code,
                        headers=response.headers,
                        content=b"".join(chunks),
                        request=response.request,
                    )
        except httpx.TransportError:
            raise failure(
                ProviderErrorCode.UNKNOWN_SIDE_EFFECT
                if method == "POST"
                else ProviderErrorCode.TEMPORARILY_UNAVAILABLE
            ) from None
        if download and result.status_code in {401, 403, 404}:
            return result  # Only a bounded metadata refresh may recover an expired signed URL.
        if not 200 <= result.status_code < 300:
            request_id = safe_id(result.headers.get("x-request-id"))
            code = (
                ProviderErrorCode.AUTH_EXPIRED
                if result.status_code in {401, 403}
                else (
                    ProviderErrorCode.RATE_LIMITED
                    if result.status_code == 429
                    else ProviderErrorCode.UNKNOWN_SIDE_EFFECT
                    if method == "POST"
                    and (
                        result.status_code >= 500
                        or result.status_code in {408, 409}
                        or 300 <= result.status_code < 400
                    )
                    else ProviderErrorCode.TEMPORARILY_UNAVAILABLE
                    if result.status_code >= 500
                    else ProviderErrorCode.VALIDATION_FAILED
                )
            )
            wait = result.headers.get("retry-after", "")
            retry_after = timedelta(seconds=min(int(wait), 86400)) if wait.isdigit() else None
            raise failure(code, request_id=request_id, retry_after=retry_after)
        return result

    def _json(self, response: httpx.Response, *, sending: bool = False) -> dict[str, Any]:
        try:
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except ValueError:
            raise failure(
                ProviderErrorCode.UNKNOWN_SIDE_EFFECT if sending else ProviderErrorCode.VALIDATION_FAILED,
                request_id=safe_id(response.headers.get("x-request-id")),
            ) from None

    def _download_url(self, raw: dict[str, Any]) -> str:
        try:
            url = httpx.URL(raw["download_url"])
            expires = datetime.fromisoformat(raw["expires_at"].replace("Z", "+00:00"))
            if expires.tzinfo is None or expires <= self.clock.now():
                raise failure(ProviderErrorCode.TEMPORARILY_UNAVAILABLE)
            if (
                url.scheme != "https"
                or url.host not in self.binding.download_hosts
                or url.port not in {None, 443}
                or url.userinfo
                or url.fragment
            ):
                raise failure()
            return str(url)
        except (KeyError, TypeError, ValueError):
            raise failure() from None

    async def describe_capabilities(self) -> ProviderCapabilities:
        features = {"send_email", "get_message", "validate_connection", "vendor_idempotency_24h"}
        if self.binding.inbox_addresses:
            features.update(
                {
                    "verify_webhook",
                    "sync_messages",
                    "received_paginated_poll",
                    "provider_original_when_available",
                    "get_attachment",
                    "provider_attachment_references",
                }
            )
        return ProviderCapabilities(
            features=frozenset(features),
            limits={"max_recipients": 50, "max_source_bytes": self.binding.max_source_bytes},
        )

    async def connect_authorized_mailbox(
        self, ctx: ActorContext, authorization: MailboxAuthorization
    ) -> EmailResult[EmailConnectionState]:
        await self._gate(ctx, "connect_authorized_mailbox", authorization)
        raise failure(ProviderErrorCode.UNSUPPORTED_CAPABILITY)

    async def validate_connection(self, ctx: ActorContext) -> EmailResult[EmailConnectionState]:
        result = await self._request(ctx, "GET", "/domains", operation="validate_connection")
        self._json(result)
        return EmailResult[EmailConnectionState](
            **self._observation(ctx, safe_id(result.headers.get("x-request-id"))),
            value=EmailConnectionState(connected=True, mode="TRANSACTIONAL"),
        )

    async def send_email(self, ctx: ActorContext, email: OutboundEmail) -> DeliveryAttempt:
        if not self.send_enabled:
            raise failure(ProviderErrorCode.UNSUPPORTED_CAPABILITY)
        if (email.tenant_id, email.provider_connection_id) != (
            self.tenant_id,
            self.connection_id,
        ) or email.provider_thread_ref is not None:
            raise failure()
        if self.binding.sender_addresses and email.sender.casefold() not in {
            value.casefold() for value in self.binding.sender_addresses
        }:
            raise failure(ProviderErrorCode.AUTH_EXPIRED)
        if len(email.to) + len(email.cc) + len(email.bcc) > 50:
            raise failure()
        headers = {
            key: value
            for key, value in {
                "Message-ID": email.message_id,
                "In-Reply-To": email.in_reply_to,
                "References": " ".join(email.references) or None,
            }.items()
            if value
        }
        payload: dict[str, Any] = dict(
            **{"from": email.sender},
            to=list(email.to),
            cc=list(email.cc),
            bcc=list(email.bcc),
            subject=email.subject,
            text=email.text_body,
            html=email.html_body,
            headers=headers,
        )
        if email.attachments:
            if self.attachment_resolver is None:
                raise failure(ProviderErrorCode.UNSUPPORTED_CAPABILITY)
            await self._gate(ctx, "resolve_outbound_attachments", email)
            attachments = []
            total = 0
            for item in email.attachments:
                content = await self.attachment_resolver(ctx, item)
                total += len(content)
                if (
                    total > self.binding.max_source_bytes
                    or (item.integrity_sha256 and sha256(content).hexdigest() != item.integrity_sha256)
                    or (item.size_bytes is not None and len(content) != item.size_bytes)
                ):
                    raise failure()
                if item.disposition == "inline" and (not item.content_id or len(item.content_id) >= 128):
                    raise failure()
                attachment: dict[str, Any] = dict(
                    filename=item.filename, content=base64.b64encode(content).decode()
                )
                if item.content_id:
                    attachment["content_id"] = item.content_id
                attachments.append(attachment)
            payload["attachments"] = attachments
        response = await self._request(
            ctx,
            "POST",
            "/emails",
            operation="send_email",
            material=email,
            payload=payload,
            key=f"aco:{self.tenant_id}:{self.connection_id}:{email.operation_id}",
        )
        value = self._json(response, sending=True)
        try:
            native = UUID(value["id"])
        except (KeyError, TypeError, ValueError):
            raise failure(
                ProviderErrorCode.UNKNOWN_SIDE_EFFECT,
                request_id=safe_id(response.headers.get("x-request-id")),
            ) from None
        return DeliveryAttempt(
            **self._observation(ctx, safe_id(response.headers.get("x-request-id"))),
            operation_id=email.operation_id,
            status="ACCEPTED",
            message_ref=self._ref(f"sent:{native}"),
        )

    async def get_message(
        self, ctx: ActorContext | EmailIngressContext, message_ref: ProviderReference
    ) -> EmailResult[EmailMessage]:
        direction, native = self._native(message_ref)
        if isinstance(ctx, EmailIngressContext) and ctx.message_id != message_ref.external_id:
            raise failure()
        path = f"/emails/receiving/{native}" if direction == "received" else f"/emails/{native}"
        source = EmailSource(kind="UNAVAILABLE")
        response: httpx.Response
        value: dict[str, Any]
        for attempt in range(2):
            response = await self._request(
                ctx,
                "GET",
                path,
                operation="get_message",
                material=message_ref,
                params={"html_format": "cid"} if direction == "received" else None,
            )
            try:
                value = EmailEnvelope.model_validate_json(response.content).model_dump(
                    mode="json", by_alias=True
                )
            except ValidationError:
                raise failure() from None
            if value.get("id") != str(native):
                raise failure()
            if direction == "received":
                recipients = {item.casefold() for item in value.get("to", [])}
                if not recipients or not recipients.issubset(
                    {item.casefold() for item in self.binding.inbox_addresses}
                ):
                    raise failure()
            raw = value.get("raw")
            if not raw or direction == "sent":
                break
            try:
                url = self._download_url(raw)
            except EmailFailure as error:
                if attempt == 0 and error.failure.code == ProviderErrorCode.TEMPORARILY_UNAVAILABLE:
                    continue
                raise
            downloaded = await self._request(
                ctx, "GET", url, operation="download_original", material=message_ref, download=True
            )
            if downloaded.status_code != 200:
                if attempt == 0:
                    continue
                raise failure(ProviderErrorCode.TEMPORARILY_UNAVAILABLE)
            source = EmailSource(
                kind="PROVIDER_ORIGINAL",
                content=downloaded.content,
                integrity_sha256=sha256(downloaded.content).hexdigest(),
            )
            break
        try:
            headers = tuple((str(k), str(v)) for k, v in (value.get("headers") or {}).items())
            if source.content is not None:
                mime = BytesParser(policy=policy.default).parsebytes(source.content)
                headers = tuple(mime.raw_items())
            normalized = {name.lower(): text for name, text in headers}
            attachments = tuple(
                EmailAttachment(
                    filename=item.get("filename") or "attachment",
                    content_type=item["content_type"],
                    reference=EmailObjectReference(
                        provider_ref=self._ref(f"received:{native}:attachment:{UUID(item['id'])}")
                    ),
                    size_bytes=item.get("size"),
                    disposition=item.get("content_disposition") or "attachment",
                    content_id=item.get("content_id"),
                )
                for item in value.get("attachments", [])
            )
            message = EmailMessage(
                tenant_id=self.tenant_id,
                provider_connection_id=self.connection_id,
                provider_ref=message_ref,
                native_identity="SIMULATED" if self.binding.evidence_mode == "MOCKED_HTTP" else "PROVIDER",
                synthetic=self.binding.evidence_mode == "MOCKED_HTTP",
                authenticity="SIMULATED" if self.binding.evidence_mode == "MOCKED_HTTP" else "VERIFIED",
                direction="incoming" if direction == "received" else "outgoing",
                kind="CUSTOMER_REPLY" if direction == "received" else "OUTBOUND_ECHO",
                sender=value.get("from"),
                to=tuple(value.get("to", [])),
                cc=tuple(value.get("cc", [])),
                bcc=tuple(value.get("bcc", [])),
                subject=value.get("subject") or "",
                text_body=value.get("text"),
                html_body=value.get("html"),
                attachments=attachments,
                message_id=value.get("message_id"),
                headers=headers,
                in_reply_to=normalized.get("in-reply-to"),
                references=tuple(normalized.get("references", "").split()),
                source=source,
                received_at=datetime.fromisoformat(value["created_at"].replace("Z", "+00:00"))
                if direction == "received"
                else None,
                sent_at=datetime.fromisoformat(value["created_at"].replace("Z", "+00:00"))
                if direction == "sent"
                else None,
            )
        except (KeyError, TypeError, ValueError, ValidationError):
            raise failure() from None
        return EmailResult[EmailMessage](
            **self._observation(ctx, safe_id(response.headers.get("x-request-id"))), value=message
        )

    async def fetch_thread(
        self, ctx: ActorContext, thread_ref: ProviderReference
    ) -> EmailResult[tuple[EmailMessage, ...]]:
        await self._gate(ctx, "fetch_thread", thread_ref)
        raise failure(ProviderErrorCode.UNSUPPORTED_CAPABILITY)

    async def sync_messages(
        self, ctx: ActorContext, cursor: EmailSyncCursor | None = None
    ) -> EmailResult[EmailSyncPage]:
        params = {"to": ",".join(self.binding.inbox_addresses), "limit": "20"}
        if cursor is not None:
            if (cursor.tenant_id, cursor.provider_connection_id) != (
                self.tenant_id,
                self.connection_id,
            ) or len(cursor.token) > 1024:
                raise failure()
            try:
                saved = json.loads(base64.urlsafe_b64decode(cursor.token))
                if saved["inboxes"] != list(self.binding.inbox_addresses):
                    raise ValueError()
                if saved["after"] is not None:
                    params["after"] = str(UUID(saved["after"]))
            except (KeyError, TypeError, ValueError):
                raise failure() from None
        response = await self._request(
            ctx, "GET", "/emails/receiving", operation="sync_messages", material=cursor, params=params
        )
        value = self._json(response)
        try:
            if (
                not isinstance(value["has_more"], bool)
                or not isinstance(value["data"], list)
                or len(value["data"]) > 20
            ):
                raise ValueError()
            ids = [str(UUID(row["id"])) for row in value["data"]]
            if len(ids) != len(set(ids)) or (value["has_more"] and not ids):
                raise ValueError()
            messages = tuple(
                [(await self.get_message(ctx, self._ref(f"received:{native}"))).value for native in ids]
            )
            token = base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "inboxes": list(self.binding.inbox_addresses),
                        "after": ids[-1] if value["has_more"] else None,
                    },
                    separators=(",", ":"),
                ).encode()
            ).decode()
            # Terminal pages restart at the head on next poll; canonical message IDs deduplicate.
            # This is bounded received polling, not a complete-history/incremental watermark claim.
            page = EmailSyncPage(
                messages=messages,
                notifications=(),
                next_cursor=EmailSyncCursor(
                    tenant_id=self.tenant_id, provider_connection_id=self.connection_id, token=token
                ),
            )
        except (KeyError, TypeError, ValueError, ValidationError):
            raise failure() from None
        return EmailResult[EmailSyncPage](
            **self._observation(ctx, safe_id(response.headers.get("x-request-id"))), value=page
        )

    async def get_attachment(
        self, ctx: ActorContext | EmailIngressContext, reference: ProviderReference
    ) -> EmailResult[bytes]:
        if (reference.provider, reference.connection_id) != (self.provider, self.connection_id):
            raise failure()
        try:
            direction, native_text, marker, attachment_text = reference.external_id.split(":")
            native, attachment_id = UUID(native_text), UUID(attachment_text)
            if direction != "received" or marker != "attachment":
                raise ValueError()
        except ValueError:
            raise failure() from None
        parent_ref = self._ref(f"received:{native}")
        if isinstance(ctx, EmailIngressContext) and ctx.message_id != parent_ref.external_id:
            raise failure()
        parent_response = await self._request(
            ctx,
            "GET",
            f"/emails/receiving/{native}",
            operation="get_message",
            material=parent_ref,
            params={"html_format": "cid"},
        )
        try:
            parent = EmailEnvelope.model_validate_json(parent_response.content).model_dump(
                mode="json", by_alias=True
            )
        except ValidationError:
            raise failure() from None
        if parent.get("id") != str(native) or not set(parent.get("to", [])).intersection(
            self.binding.inbox_addresses
        ):
            raise failure()
        if str(attachment_id) not in {item.get("id") for item in parent.get("attachments", [])}:
            raise failure()
        for attempt in range(2):
            response = await self._request(
                ctx,
                "GET",
                f"/emails/receiving/{native}/attachments/{attachment_id}",
                operation="get_attachment",
                material=reference,
            )
            value = self._json(response)
            if value.get("id") != str(attachment_id):
                raise failure()
            try:
                url = self._download_url(value)
            except EmailFailure as error:
                if attempt == 0 and error.failure.code == ProviderErrorCode.TEMPORARILY_UNAVAILABLE:
                    continue
                raise
            downloaded = await self._request(
                ctx, "GET", url, operation="download_attachment", material=reference, download=True
            )
            if downloaded.status_code != 200:
                if attempt == 0:
                    continue
                raise failure(ProviderErrorCode.TEMPORARILY_UNAVAILABLE)
            if type(value.get("size")) is not int or len(downloaded.content) != value["size"]:
                raise failure()
            return EmailResult[bytes](
                **self._observation(ctx, safe_id(response.headers.get("x-request-id"))),
                value=downloaded.content,
            )
        raise failure(ProviderErrorCode.TEMPORARILY_UNAVAILABLE)

    async def subscribe_or_renew_notifications(
        self, ctx: ActorContext, request: NotificationSubscriptionRequest
    ) -> EmailResult[EmailSubscription]:
        await self._gate(ctx, "subscribe_or_renew_notifications", request)
        raise failure(ProviderErrorCode.UNSUPPORTED_CAPABILITY)

    async def disconnect(self, ctx: ActorContext) -> EmailResult[EmailConnectionState]:
        await self._gate(ctx, "disconnect")
        # Canonical connection revocation belongs to the application connection service.
        # No provider key revocation API is implied by a local adapter object.
        raise failure(ProviderErrorCode.UNSUPPORTED_CAPABILITY)

    async def verify_webhook(
        self, ctx: ActorContext | EmailWebhookContext, raw_body: bytes, headers: Mapping[str, str]
    ) -> EmailResult[tuple[EmailNotification, ...]]:
        from aco.integrations.resend.webhooks import verified_notifications

        if not isinstance(ctx, EmailWebhookContext):
            raise failure(ProviderErrorCode.UNSUPPORTED_CAPABILITY)
        await self._gate(ctx, "verify_webhook", ctx.raw_sha256)
        values = verified_notifications(self.binding, ctx, raw_body, headers)
        return EmailResult[tuple[EmailNotification, ...]](**self._observation(ctx), value=values)
