"""Real maintained S3 adapter contract; no paid/external business providers."""

import hashlib
from datetime import tzinfo
from typing import Self, SupportsIndex
from uuid import uuid4

import pytest


async def test_storage_port_roundtrip_identity_and_fail_closed_scope() -> None:
    # Break caught: unencrypted upload, wrong-tenant key use, corrupt bytes or lost identity.
    from artifact_support import storage_adapter

    from aco.integrations.ports.storage import ObjectWrite, StorageFailure

    adapter, ctx = storage_adapter()
    body = b"Synthetic storage port roundtrip"
    command = ObjectWrite(
        artifact_id=uuid4(), sha256=hashlib.sha256(body).hexdigest(), content_type="text/plain", body=body
    )
    result = await adapter.put_private_artifact(ctx, command)
    ref = result.value
    assert ref.sha256 == command.sha256
    assert ref.encryption == "AES256"
    assert (await adapter.read_private_artifact(ctx, ref)).value == body
    assert (await adapter.verify_integrity(ctx, ref)).value is True
    with pytest.raises(StorageFailure):
        await adapter.get_metadata(ctx.model_copy(update={"tenant_id": uuid4()}), ref)
    assert (await adapter.delete_object(ctx, ref)).value is True
    with pytest.raises(StorageFailure):
        await adapter.read_private_artifact(ctx, ref)


async def test_internal_url_ttl_is_bounded_and_capabilities_are_explicit() -> None:
    from artifact_support import storage_adapter

    from aco.integrations.ports.storage import ObjectWrite, StorageFailure

    adapter, ctx = storage_adapter()
    body = b"Private signed synthetic"
    command = ObjectWrite(
        artifact_id=uuid4(), sha256=hashlib.sha256(body).hexdigest(), content_type="text/plain", body=body
    )
    ref = (await adapter.put_private_artifact(ctx, command)).value
    capabilities = await adapter.describe_capabilities()
    assert {"private", "encryption", "expiry", "provider_purge"} <= capabilities.features
    for ttl in (0, 61):
        with pytest.raises(StorageFailure):
            await adapter.get_signed_read_url(ctx, ref, ttl)
    result = await adapter.get_signed_read_url(ctx, ref, 1)
    assert result.provider_attempted is False
    assert result.native_operation_id is None
    assert result.cost.amount is None
    await adapter.delete_object(ctx, ref)


async def test_missing_encryption_key_blocks_storage() -> None:
    """Application fail-closed proof; real missing-key infrastructure is a separate root lane."""
    from artifact_support import storage_adapter
    from botocore.stub import Stubber

    from aco.integrations.ports.storage import ObjectWrite, StorageFailure

    adapter, ctx = storage_adapter()
    body = b"Synthetic encryption rejection"
    with Stubber(adapter.client) as stub:
        stub.add_response(
            "get_bucket_encryption",
            {"ServerSideEncryptionConfiguration": {"Rules": []}},
            {"Bucket": adapter.bucket},
        )
        with pytest.raises(StorageFailure) as error:
            await adapter.put_private_artifact(
                ctx,
                ObjectWrite(
                    artifact_id=uuid4(),
                    sha256=hashlib.sha256(body).hexdigest(),
                    content_type="text/plain",
                    body=body,
                ),
            )
        assert error.value.failure.code.value == "UNSUPPORTED_CAPABILITY"
        assert error.value.reason == "ENCRYPTION_UNVERIFIED"
        stub.assert_no_pending_responses()


async def test_real_invalid_credentials_are_typed_and_sanitized() -> None:
    from artifact_support import storage_adapter, storage_configuration
    from pydantic import SecretStr

    from aco.integrations.ports.storage import ObjectWrite, StorageFailure
    from aco.integrations.s3.adapter import S3StorageAdapter

    adapter, ctx = storage_adapter()
    rejected_configuration = storage_configuration()
    rejected_configuration["access_key"] = SecretStr("invalid-synthetic-access-key")
    rejected_configuration["secret_key"] = SecretStr("invalid-synthetic-secret")
    rejected = S3StorageAdapter(
        tenant_id=ctx.tenant_id, connection_id=adapter.connection_id, **rejected_configuration
    )
    with pytest.raises(StorageFailure) as error:
        await rejected.put_private_artifact(
            ctx,
            ObjectWrite(
                artifact_id=uuid4(),
                sha256=hashlib.sha256(b"x").hexdigest(),
                content_type="text/plain",
                body=b"x",
            ),
        )
    assert error.value.failure.code.value == "AUTH_EXPIRED"
    assert str(error.value) == "AUTH_EXPIRED"
    assert not error.value.failure.retryable and not error.value.failure.may_have_side_effect


async def test_interrupted_sdk_stream_is_typed_unavailable() -> None:
    import io

    from urllib3.connectionpool import HTTPConnectionPool
    from urllib3.exceptions import ReadTimeoutError

    class Interrupted(io.BytesIO):
        def read(self, amount: SupportsIndex | None = None) -> bytes:
            raise ReadTimeoutError(
                HTTPConnectionPool("synthetic.invalid"), None, "synthetic transport timeout"
            )

    from artifact_support import storage_adapter
    from botocore.response import StreamingBody
    from botocore.stub import Stubber

    from aco.integrations.ports.storage import ArtifactObjectRef, StorageFailure
    from aco.integrations.s3.adapter import object_key

    adapter, ctx = storage_adapter()
    artifact_id = uuid4()
    digest = hashlib.sha256(b"valid").hexdigest()
    ref = ArtifactObjectRef(
        tenant_id=ctx.tenant_id,
        connection_id=adapter.connection_id,
        artifact_id=artifact_id,
        key=object_key(ctx.tenant_id, artifact_id, digest),
        sha256=digest,
        content_type="text/plain",
        size_bytes=5,
        version=None,
        encryption="AES256",
    )
    response = {
        "ContentLength": 5,
        "ContentType": "text/plain",
        "Metadata": {"sha256": digest},
        "ServerSideEncryption": "AES256",
    }
    with Stubber(adapter.client) as stub:
        stub.add_response("head_object", response, {"Bucket": adapter.bucket, "Key": ref.key})
        stub.add_response(
            "get_object",
            {**response, "Body": StreamingBody(Interrupted(b"valid"), 5)},
            {"Bucket": adapter.bucket, "Key": ref.key},
        )
        with pytest.raises(StorageFailure) as error:
            await adapter.read_private_artifact(ctx, ref)
        assert error.value.failure.code.value == "TEMPORARILY_UNAVAILABLE"
        assert not error.value.failure.may_have_side_effect


@pytest.mark.parametrize(
    ("service_code", "status", "mutation", "expected_code", "retryable", "uncertain"),
    [
        ("SlowDown", 429, False, "RATE_LIMITED", False, False),
        ("ServiceUnavailable", 503, False, "TEMPORARILY_UNAVAILABLE", True, False),
        ("ServiceUnavailable", 503, True, "UNKNOWN_SIDE_EFFECT", False, True),
        ("NoSuchKey", 404, False, "VALIDATION_FAILED", False, False),
        ("AccessDenied", 403, False, "AUTH_EXPIRED", False, False),
        ("NotImplemented", 501, False, "UNSUPPORTED_CAPABILITY", False, False),
    ],
)
def test_sdk_error_preserves_retry_after_and_failure_policy(
    service_code: str, status: int, mutation: bool, expected_code: str, retryable: bool, uncertain: bool
) -> None:
    # Break caught: losing provider delay/identity or treating delay as replay authority.
    from datetime import timedelta

    from artifact_support import storage_adapter
    from botocore.stub import Stubber

    from aco.integrations.ports.storage import StorageFailure

    adapter, _ = storage_adapter()
    method = "put_object" if mutation else "head_object"
    with Stubber(adapter.client) as stub:
        stub.add_client_error(
            method,
            service_error_code=service_code,
            http_status_code=status,
            expected_params={"Bucket": adapter.bucket, "Key": "synthetic"},
            response_meta={
                "RequestId": "synthetic-rate-limit",
                "HTTPHeaders": {"retry-after": "17"},
            },
        )
        with pytest.raises(StorageFailure) as caught:
            adapter._sdk(method, mutation=mutation, Key="synthetic")
        stub.assert_no_pending_responses()
    normalized = caught.value.failure
    assert normalized.code.value == expected_code
    assert normalized.request_id == "synthetic-rate-limit"
    assert normalized.retryable is retryable
    assert normalized.may_have_side_effect is uncertain
    assert normalized.retry_after == timedelta(seconds=17)


@pytest.mark.parametrize("mutation", [False, True], ids=["read", "uncertain-mutation"])
@pytest.mark.parametrize(
    ("header", "expected_seconds"),
    [
        pytest.param("0", 0, id="zero"),
        pytest.param(" 17 ", 17, id="whitespace"),
        pytest.param("Fri, 01 Jan 2027 00:00:17 GMT", 17, id="http-date"),
        pytest.param("Thu, 31 Dec 2026 23:59:59 GMT", 0, id="past-date"),
        pytest.param(None, None, id="missing"),
        pytest.param("", None, id="empty"),
        pytest.param("-1", None, id="negative"),
        pytest.param("1.5", None, id="fraction"),
        pytest.param("not a date", None, id="malformed"),
        pytest.param("Fri, 01 Jan 2027 00:00:17", None, id="missing-timezone"),
        pytest.param("999999999999999999999999999999999999", None, id="overflow"),
        pytest.param(17, None, id="non-string"),
    ],
)
def test_sdk_retry_after_metadata_is_bounded_and_does_not_authorize_replay(
    monkeypatch: pytest.MonkeyPatch, mutation: bool, header: str | int | None, expected_seconds: int | None
) -> None:
    # Break caught: crashing error normalization, wrong date delta, or changing mutation safety.
    from datetime import UTC, datetime, timedelta

    from artifact_support import storage_adapter
    from botocore.stub import Stubber

    from aco.integrations.ports.storage import StorageFailure
    from aco.integrations.s3 import adapter as adapter_module

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls(2027, 1, 1, tzinfo=UTC)

    # raising=False also reaches the current implementation before its clock import exists.
    monkeypatch.setattr(adapter_module, "datetime", FixedDatetime, raising=False)
    adapter, _ = storage_adapter()
    method = "put_object" if mutation else "head_object"
    headers = {} if header is None else {"Retry-After": header}
    with Stubber(adapter.client) as stub:
        stub.add_client_error(
            method,
            service_error_code="ServiceUnavailable",
            http_status_code=503,
            expected_params={"Bucket": adapter.bucket, "Key": "synthetic"},
            response_meta={"RequestId": "synthetic-unavailable", "HTTPHeaders": headers},
        )
        with pytest.raises(StorageFailure) as caught:
            adapter._sdk(method, mutation=mutation, Key="synthetic")
        stub.assert_no_pending_responses()
    normalized = caught.value.failure
    assert normalized.code.value == ("UNKNOWN_SIDE_EFFECT" if mutation else "TEMPORARILY_UNAVAILABLE")
    assert normalized.request_id == "synthetic-unavailable"
    assert normalized.retryable is (not mutation)
    assert normalized.may_have_side_effect is mutation
    assert normalized.retry_after == (
        None if expected_seconds is None else timedelta(seconds=expected_seconds)
    )
