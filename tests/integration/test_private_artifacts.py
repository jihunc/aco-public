"""Production artifact API, isolated PostgreSQL and pinned local private S3."""

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NoReturn
from uuid import UUID, uuid4

import pytest
from artifact_support import storage_runtime
from boundary_contracts import boundary_inventory
from boundary_evidence import BoundaryObserver

from aco.core.context import ActorContext
from aco.integrations.ports.storage import ArtifactObjectRef, ObjectWrite
from aco.integrations.s3.adapter import S3StorageAdapter


def test_boundary_artifact_limits_and_tenancy(boundary_observer: BoundaryObserver) -> None:
    # Break caught: permitting oversize media, silently losing rejected originals,
    # or returning another tenant's raw private artifact.
    from artifact_support import artifact_world, upload

    ids = boundary_inventory()["artifact_basic"]["case_ids"]
    cases = [
        c
        for c in json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]
        if c["id"] in ids
    ]
    seen = set()
    with artifact_world() as (client, store, tenant):
        for case in cases:
            seen.add(case["id"])
            if "size_bytes" in case["input"]:
                response = upload(client, tenant, b"X" * case["input"]["size_bytes"], "application/pdf")
                assert response.status_code == 201
                value = response.json()
                actual = {"within_demo_size_limit": value["storage_state"] == "stored"}
                if case["id"] == "artifact_oversize_visible_failure":
                    actual.update(
                        artifact_state=value["storage_state"], silently_discard_original=value["id"] is None
                    )
                boundary_observer.assert_observed(case["id"], actual)
                expected = case["expected"]
                assert (value["storage_state"] == "stored") == expected["within_demo_size_limit"]
                if not expected["within_demo_size_limit"]:
                    assert value["storage_state"] == expected["artifact_state"]
                    assert value["sha256"] == hashlib.sha256(b"X" * case["input"]["size_bytes"]).hexdigest()
                    assert value["failure_code"] == "MEDIA_REJECTED"
                    assert value["source_reference"]
                    assert (value["id"] is None) == expected["silently_discard_original"]
            else:
                value = upload(client, tenant, b"Synthetic private evidence").json()
                from urllib.parse import parse_qs, urlsplit

                import anyio
                import httpx

                from aco.core.context import TenantContext
                from aco.core.types import Role
                from aco.features.artifacts.models import Artifact
                from aco.features.artifacts.service import object_ref
                from aco.features.identity.models import Membership, Tenant

                actor = store.query(Membership).filter(Membership.tenant_id == tenant).one().user_id
                row = store.get(Artifact, value["id"])
                assert row is not None
                adapter = storage_runtime(client).bind(tenant, row.provider_connection_id)
                owner = TenantContext(
                    tenant_id=tenant, actor_id=actor, role=Role.OWNER, correlation_id=uuid4()
                )
                signed = anyio.run(adapter.get_signed_read_url, owner, object_ref(row), 60)
                assert case["input"]["internal_signed_url_not_expired"] is True
                assert parse_qs(urlsplit(signed.value).query)["X-Amz-Expires"] == ["60"]
                try:
                    internal = httpx.get(signed.value, trust_env=False, timeout=5)
                except httpx.HTTPError:
                    raise AssertionError("Internal synthetic signed read failed") from None
                assert internal.status_code == 200
                assert hashlib.sha256(internal.content).hexdigest() == value["sha256"]
                foreign = Tenant(name="Foreign boundary", timezone="UTC")
                store.add(foreign)
                store.flush()
                store.add(Membership(tenant_id=foreign.id, user_id=actor, role="owner"))
                store.commit()
                response = client.get(f"/api/workspaces/{foreign.id}/artifacts/{value['id']}/content")
                assert response.status_code == case["expected"]["http_status"]
                assert (response.status_code == 200) == case["expected"]["download_allowed"]
                boundary_observer.assert_observed(
                    case["id"],
                    {"download_allowed": response.status_code == 200, "http_status": response.status_code},
                )
    assert seen == {
        "artifact_max_size_boundary",
        "artifact_oversize_visible_failure",
        "artifact_proxy_rejects_wrong_tenant",
    }


def test_checksum_and_retry_preserve_identity() -> None:
    from artifact_support import artifact_world, upload

    with artifact_world() as (client, store, tenant):
        source = str(uuid4())
        first = upload(client, tenant, b"Original synthetic MIME", source=source)
        assert first.status_code == 201
        second = upload(client, tenant, b"Original synthetic MIME", source=source)
        assert second.json()["id"] == first.json()["id"]
        assert second.json()["sha256"] == hashlib.sha256(b"Original synthetic MIME").hexdigest()
        assert upload(client, tenant, b"Changed evidence", source=source).status_code == 409
        result = client.get(f"/api/workspaces/{tenant}/artifacts/{first.json()['id']}/content")
        assert result.content == b"Original synthetic MIME"
        assert result.headers["x-content-type-options"] == "nosniff"
        assert result.headers["content-disposition"].startswith("attachment")


def test_wrong_tenant_cannot_issue_or_fetch_artifact() -> None:
    from artifact_support import artifact_world, upload

    from aco.features.identity.models import Membership, Tenant

    with artifact_world() as (client, store, tenant):
        value = upload(client, tenant, b"Synthetic private").json()
        other = Tenant(name="Other", timezone="UTC")
        store.add(other)
        store.flush()
        member = store.query(Membership).filter(Membership.tenant_id == tenant).one()
        store.add(Membership(tenant_id=other.id, user_id=member.user_id, role="owner"))
        store.commit()
        for suffix in ("", "/content"):
            assert (
                client.get(f"/api/workspaces/{other.id}/artifacts/{value['id']}{suffix}").status_code == 404
            )


def test_expired_or_deleted_link_fails() -> None:
    from datetime import timedelta

    from artifact_support import artifact_world, upload

    from aco.features.artifacts.models import Artifact
    from aco.features.identity.models import now

    with artifact_world() as (client, store, tenant):
        value = upload(client, tenant, b"Synthetic expiring").json()
        row = store.get(Artifact, value["id"])
        assert row is not None
        row.expires_at = now() - timedelta(seconds=1)
        store.commit()
        url = f"/api/workspaces/{tenant}/artifacts/{value['id']}/content"
        assert client.get(url).status_code == 404
        store.expire_all()
        assert store.get_one(Artifact, value["id"]).storage_state == "purge_pending"
        value = upload(client, tenant, b"Synthetic deleting").json()
        url = f"/api/workspaces/{tenant}/artifacts/{value['id']}"
        response = client.request("DELETE", url, json={"reason": "synthetic privacy request"})
        assert response.status_code == 200
        assert response.json()["status"] in {"deleted", "purge_pending"}
        assert client.get(url + "/content").status_code == 404


def test_failed_upload_does_not_claim_original_is_archived(monkeypatch: pytest.MonkeyPatch) -> None:
    from artifact_support import artifact_world, upload
    from sqlalchemy import select

    from aco.core.types import ProviderErrorCode, ProviderFailure
    from aco.features.artifacts.models import Artifact
    from aco.integrations.ports.storage import StorageFailure

    with artifact_world() as (client, store, tenant):
        runtime = storage_runtime(client)
        original = runtime.bind

        def unavailable(tenant_id: UUID, connection_id: UUID) -> S3StorageAdapter:
            adapter = original(tenant_id, connection_id)

            async def reject(ctx: ActorContext, command: ObjectWrite) -> NoReturn:
                raise StorageFailure(
                    ProviderFailure(
                        code=ProviderErrorCode.TEMPORARILY_UNAVAILABLE,
                        retryable=True,
                        may_have_side_effect=False,
                    )
                )

            monkeypatch.setattr(adapter, "put_private_artifact", reject)
            return adapter

        monkeypatch.setattr(runtime, "bind", unavailable)
        source = str(uuid4())
        response = upload(client, tenant, b"Synthetic upload failure", source=source)
        assert response.status_code == 503
        row = store.scalar(select(Artifact).where(Artifact.source_reference == source))
        assert row is not None and row.storage_state != "stored"
        assert row.sha256 == hashlib.sha256(b"Synthetic upload failure").hexdigest()
        monkeypatch.setattr(runtime, "bind", original)
        retry = upload(client, tenant, b"Synthetic upload failure", source=source)
        assert retry.status_code == 201
        assert retry.json()["id"] == str(row.id)


def test_recovery_reconciles_object_database_partial_failure() -> None:
    from artifact_support import artifact_world, upload
    from sqlalchemy import event, select
    from sqlalchemy.orm import Session

    from aco.features.artifacts.models import Artifact
    from aco.features.usage.models import UsageAttempt

    with artifact_world() as (client, store, tenant):
        source = str(uuid4())

        def lose_commit(session: Session) -> None:
            if any(
                isinstance(row, Artifact) and row.source_reference == source and row.storage_state == "stored"
                for row in session.identity_map.values()
            ):
                raise RuntimeError("synthetic final artifact commit failure")

        event.listen(Session, "before_commit", lose_commit)
        try:
            with pytest.raises(RuntimeError, match="synthetic final artifact commit failure"):
                upload(client, tenant, b"Synthetic durable original", source=source)
        finally:
            event.remove(Session, "before_commit", lose_commit)
        row = store.scalar(select(Artifact).where(Artifact.source_reference == source))
        assert row and row.storage_state == "pending"
        attempt = store.scalar(select(UsageAttempt).where(UsageAttempt.operation == "put_private_artifact"))
        assert attempt is not None
        assert attempt.outcome == "VERIFIED" and attempt.business_outcome == "ROLLED_BACK"
        adapter = storage_runtime(client).bind(tenant, row.provider_connection_id)
        before = adapter.client.head_object(Bucket=adapter.bucket, Key=row.object_key)
        result = upload(client, tenant, b"Synthetic durable original", source=source)
        assert result.status_code == 201 and result.json()["id"] == str(row.id)
        assert result.json()["storage_state"] == "stored"
        after = adapter.client.head_object(Bucket=adapter.bucket, Key=row.object_key)
        assert before["ETag"] == after["ETag"]
        assert before["LastModified"] == after["LastModified"]
        store.expire_all()
        assert len(store.scalars(select(Artifact).where(Artifact.source_reference == source)).all()) == 1
        assert (
            client.get(f"/api/workspaces/{tenant}/artifacts/{row.id}/content").content
            == b"Synthetic durable original"
        )


def test_viewer_read_rechecks_membership_and_owner_delete_authority() -> None:
    from artifact_support import artifact_world, upload
    from sqlalchemy import select

    from aco.features.identity.models import Membership

    with artifact_world() as (client, store, tenant):
        value = upload(client, tenant, b"Authorized viewer synthetic").json()
        member = store.scalar(select(Membership).where(Membership.tenant_id == tenant))
        assert member is not None
        member.role = "viewer"
        store.commit()
        path = f"/api/workspaces/{tenant}/artifacts/{value['id']}"
        assert client.get(path + "/content").status_code == 200
        assert client.request("DELETE", path, json={"reason": "request"}).status_code == 403
        member.active = False
        store.commit()
        assert client.get(path + "/content").status_code == 403


def test_real_capture_and_unverified_legal_hold_fail_closed() -> None:
    from artifact_support import artifact_world, upload

    from aco.features.artifacts.models import Artifact
    from aco.features.identity.models import Tenant

    with artifact_world() as (client, store, tenant):
        value = upload(client, tenant, b"Hold synthetic").json()
        row = store.get(Artifact, value["id"])
        assert row is not None
        row.legal_hold_state = "unknown"
        store.commit()
        path = f"/api/workspaces/{tenant}/artifacts/{value['id']}"
        assert client.request("DELETE", path, json={"reason": "request"}).status_code == 409
        assert client.get(path + "/content").status_code == 404
        store.get_one(Tenant, tenant).is_demo = False
        store.commit()
        assert upload(client, tenant, b"Real policy absent").status_code == 403


def test_retention_queue_purge_and_canonical_usage_are_real() -> None:
    from datetime import timedelta

    from artifact_support import artifact_world, upload
    from sqlalchemy import select

    from aco.features.artifacts.models import Artifact, ArtifactPurge
    from aco.features.identity.models import AuditEvent, now
    from aco.features.usage.models import ProviderUsageEvent, UsageAttempt, UsageRevision

    with artifact_world() as (client, store, tenant):
        value = upload(client, tenant, b"Synthetic retention proof").json()
        row = store.get(Artifact, value["id"])
        assert row is not None
        assert row.expires_at - row.captured_at == timedelta(days=30)
        job = store.scalar(select(ArtifactPurge).where(ArtifactPurge.artifact_id == row.id))
        assert job and job.due_at <= row.expires_at + timedelta(hours=24)
        assert job.status == "scheduled"
        upload_attempt = store.scalar(
            select(UsageAttempt).where(UsageAttempt.operation == "put_private_artifact")
        )
        assert upload_attempt is not None
        assert upload_attempt.business_outcome == "COMMITTED"
        path = f"/api/workspaces/{tenant}/artifacts/{row.id}"
        assert client.get(path + "/content").status_code == 200
        row.expires_at = now() - timedelta(seconds=1)
        store.commit()
        assert client.get(path + "/content").status_code == 404
        store.expire_all()
        assert store.get_one(Artifact, row.id).revoked_at
        assert store.get_one(ArtifactPurge, job.id).status == "purge_pending"
        result = client.post(path + "/purge")
        assert result.status_code == 200
        assert result.json()["status"] == "deleted"
        assert result.json()["confirmed_at"]
        assert client.post(path + "/purge").json() == result.json()
        store.expire_all()
        events = list(
            store.scalars(select(ProviderUsageEvent).where(ProviderUsageEvent.provider_kind == "storage"))
        )
        assert [event.operation for event in events] == [
            "put_private_artifact",
            "read_private_artifact",
            "delete_object",
        ]
        for event in events:
            revision = store.scalar(select(UsageRevision).where(UsageRevision.event_id == event.id))
            assert revision is not None
            assert revision.cost_quality == "UNKNOWN" and revision.cost_amount is None
            assert event.provider_connection_id == row.provider_connection_id
            if event.native_operation_id is None:
                assert event.operation_key.startswith("attempt:")
        audits = list(store.scalars(select(AuditEvent).where(AuditEvent.target_id == row.id)))
        assert {audit.action for audit in audits} >= {
            "artifact.stored",
            "artifact.read",
            "artifact.access_revoked",
            "artifact.purge_confirmed",
        }
        assert all(audit.actor_id for audit in audits)


def test_failed_purge_stays_blocked_with_access_revoked(monkeypatch: pytest.MonkeyPatch) -> None:
    from artifact_support import artifact_world, upload

    from aco.core.types import ProviderErrorCode, ProviderFailure
    from aco.features.artifacts.models import Artifact
    from aco.integrations.ports.storage import StorageFailure

    with artifact_world() as (client, store, tenant):
        value = upload(client, tenant, b"Synthetic purge failure").json()
        runtime = storage_runtime(client)
        original = runtime.bind

        def unavailable(tenant_id: UUID, connection_id: UUID) -> S3StorageAdapter:
            adapter = original(tenant_id, connection_id)

            async def reject(ctx: ActorContext, ref: ArtifactObjectRef) -> NoReturn:
                raise StorageFailure(
                    ProviderFailure(
                        code=ProviderErrorCode.UNSUPPORTED_CAPABILITY,
                        retryable=False,
                        may_have_side_effect=False,
                    )
                )

            monkeypatch.setattr(adapter, "delete_object", reject)
            return adapter

        monkeypatch.setattr(runtime, "bind", unavailable)
        path = f"/api/workspaces/{tenant}/artifacts/{value['id']}"
        result = client.request("DELETE", path, json={"reason": "synthetic privacy request"})
        assert result.status_code == 200
        assert result.json()["status"] == "blocked"
        assert result.json()["confirmed_at"] is None
        assert result.json()["failure_code"] == "UNSUPPORTED_CAPABILITY"
        assert client.get(path + "/content").status_code == 404
        store.expire_all()
        assert store.get_one(Artifact, value["id"]).storage_state == "blocked"
        monkeypatch.setattr(runtime, "bind", original)
        assert client.post(path + "/purge").json()["status"] == "deleted"


def test_consent_revocation_immediately_denies_previously_read_artifact() -> None:
    from artifact_support import artifact_world, upload

    from aco.features.artifacts.models import Artifact

    with artifact_world() as (client, store, tenant):
        value = upload(
            client, tenant, b"Synthetic permitted transcript", content_type="application/json"
        ).json()
        # Email class cannot accept a voice transcript merely by choosing its type.
        assert value["storage_state"] == "quarantined"
        value = upload(client, tenant, b"Synthetic revocable evidence").json()
        path = f"/api/workspaces/{tenant}/artifacts/{value['id']}/content"
        assert client.get(path).status_code == 200
        store.get_one(Artifact, value["id"]).consent_revoked = True
        store.commit()
        assert client.get(path).status_code == 404


@pytest.mark.parametrize("role", ["finance_manager", "operator", "viewer"])
def test_manual_ingest_requires_current_owner(role: str) -> None:
    from artifact_support import artifact_world, upload

    with artifact_world(role) as (client, store, tenant):
        assert upload(client, tenant, b"Synthetic unauthorized original").status_code == 403


def test_binary_ref_rejects_forged_key_and_service_context() -> None:
    import anyio
    from artifact_support import storage_adapter

    from aco.core.context import ServiceContext
    from aco.integrations.ports.storage import ObjectWrite, StorageFailure

    adapter, ctx = storage_adapter()
    body = b"Synthetic scoped key"
    ref = anyio.run(
        adapter.put_private_artifact,
        ctx,
        ObjectWrite(
            artifact_id=uuid4(), sha256=hashlib.sha256(body).hexdigest(), content_type="text/plain", body=body
        ),
    ).value
    for forged in (
        ref.model_copy(update={"key": "somewhere/else"}),
        ref.model_copy(update={"connection_id": uuid4()}),
    ):
        with pytest.raises(StorageFailure):
            anyio.run(adapter.read_private_artifact, ctx, forged)
    service = ServiceContext(
        tenant_id=ctx.tenant_id,
        actor_id=ctx.actor_id,
        correlation_id=ctx.correlation_id,
        action_id=uuid4(),
        action_version=1,
    )
    with pytest.raises(StorageFailure):
        anyio.run(adapter.read_private_artifact, service, ref)
    anyio.run(adapter.delete_object, ctx, ref)


@pytest.fixture(scope="module")
def maintained_artifact() -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """Root performs infrastructure maintenance; this app lane never gains Docker IPC."""
    import os
    import re
    import secrets
    import time

    from artifact_support import artifact_world, upload

    from aco.features.artifacts.models import Artifact

    if os.environ.get("ACO_T09_MAINTENANCE") != "1":
        pytest.skip("separate Root-controlled real restart/raw-volume proof")
    directory = Path(".tooling/artifact-maintenance")
    directory.mkdir(parents=True, exist_ok=True)
    run = os.environ.get("ACO_T09_MAINTENANCE_RUN", uuid4().hex)
    assert re.fullmatch(r"[a-z0-9-]{1,40}", run)
    request_path = directory / f"t09-maintenance-{run}-request.json"
    receipt_path = directory / f"t09-maintenance-{run}-receipt.json"
    assert not request_path.exists() and not receipt_path.exists(), "Maintenance evidence paths already used"
    marker = "ACO_T09_APPLICATION_SYNTHETIC_" + uuid4().hex
    body = marker.encode() + secrets.token_bytes(65536)
    with artifact_world() as (client, store, tenant):
        response = upload(client, tenant, body)
        assert response.status_code == 201
        value = response.json()
        row = store.get(Artifact, value["id"])
        assert row is not None
        path = f"/api/workspaces/{tenant}/artifacts/{row.id}/content"
        assert client.get(path).content == body
        nonce = uuid4().hex
        request_path.write_text(
            json.dumps(
                {
                    "nonce": nonce,
                    "tenant_id": str(tenant),
                    "artifact_id": str(row.id),
                    "object_key": row.object_key,
                    "sha256": row.sha256,
                    "marker": marker,
                    "schema": store.execute(
                        __import__("sqlalchemy").text("SELECT current_schema()")
                    ).scalar_one(),
                }
            )
        )
        request_path.chmod(0o600)
        deadline = time.monotonic() + 600
        while not receipt_path.exists() and time.monotonic() < deadline:
            time.sleep(0.25)
        assert receipt_path.exists(), "Root maintenance receipt not supplied within bounded window"
        receipt = json.loads(receipt_path.read_text())
        assert receipt["nonce"] == nonce
        assert receipt["raw_plaintext_absent"] is True
        assert receipt["storage_restart_completed"] is True
        assert receipt["author"] == "Root"
        # Same committed artifact/key; production API makes a new adapter/client.
        recovered = client.get(path)
        assert recovered.status_code == 200
        assert hashlib.sha256(recovered.content).hexdigest() == value["sha256"]
        assert recovered.content == body
        store.expire_all()
        assert store.get_one(Artifact, row.id).object_key == row.object_key
        yield value, receipt
        deleted = client.request(
            "DELETE",
            f"/api/workspaces/{tenant}/artifacts/{row.id}",
            json={"reason": "completed synthetic storage proof"},
        )
        assert deleted.json()["status"] == "deleted"


def test_encrypted_artifact_round_trip_and_plaintext_absence(
    maintained_artifact: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    value, receipt = maintained_artifact
    assert value["storage_state"] == "stored"
    assert receipt["raw_plaintext_absent"] is True


def test_storage_restart_preserves_private_object(
    maintained_artifact: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    value, receipt = maintained_artifact
    assert receipt["storage_restart_completed"] is True
    assert value["sha256"]


def test_missing_encryption_key_blocks_storage() -> None:
    """Root supplies one empty, exact-routed bucket; application has data-only credentials."""
    import os

    from artifact_support import artifact_world, upload
    from sqlalchemy import select

    from aco.features.artifacts.models import Artifact

    bucket = os.environ.get("ACO_T09_EMPTY_BUCKET")
    if not bucket:
        pytest.skip("Requires Root's bounded exact-bucket missing-encryption maintenance lane")
    assert bucket.startswith("aco-t09-empty-") and len(bucket) <= 63
    with artifact_world() as (client, store, tenant):
        runtime = storage_runtime(client)
        runtime.configuration["bucket"] = bucket
        source = str(uuid4())
        response = upload(client, tenant, b"Synthetic must never be plaintext", source=source)
        assert response.status_code == 422
        assert response.json()["detail"] == "UNSUPPORTED_CAPABILITY"
        row = store.scalar(select(Artifact).where(Artifact.source_reference == source))
        assert row is not None and row.storage_state == "failed"
        assert row.failure_code == "UNSUPPORTED_CAPABILITY"
        # Root independently proves zero objects and removes the temporary bucket.
        adapter = runtime.bind(tenant, row.provider_connection_id)
        from botocore.exceptions import ClientError

        with pytest.raises(ClientError) as missing:
            adapter.client.head_object(Bucket=bucket, Key=row.object_key)
        assert missing.value.response["ResponseMetadata"]["HTTPStatusCode"] == 404
