"""Current human consent and real private artifacts; synthetic provider media only."""

from boundary_evidence import BoundaryObserver
from fastapi import FastAPI
from identity_support import actor_client


def test_voice_consent_configuration_is_current_owner_and_explicitly_synthetic() -> None:
    with actor_client("owner") as (client, _db, _actor, tenant):
        app = client.app
        assert isinstance(app, FastAPI)
        response = client.get(f"/api/workspaces/{tenant}/voice-consents")
        assert response.status_code == 200, response.text
        assert response.json()["synthetic_only"] is True
        assert response.json()["items"] == []
        assert response.json()["can_record"] is False  # no configured voice/storage scope yet


async def test_explicit_decisions_are_scoped_current_immutable_and_withdrawable() -> None:
    from datetime import timedelta
    from uuid import UUID, uuid4

    import pytest
    from action_support import action_world
    from pydantic import SecretStr
    from sqlalchemy import func, select, text
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from aco.features.accounting.models import InvoiceSnapshot
    from aco.features.artifacts.consent_models import ConsentEvidence, ConsentRevocation
    from aco.features.identity.models import Membership, ProviderConnection, now
    from aco.features.voice.api import router as voice_router
    from aco.features.voice.demo import OfflineVoiceDemo

    with action_world() as world:
        app = world.client.app
        assert isinstance(app, FastAPI)
        app.include_router(voice_router)
        engine = create_async_engine(world.url)

        async def no_callback(raw: bytes, headers: dict[str, str]) -> None:
            raise AssertionError("Consent must not start a call")

        demo = OfflineVoiceDemo(
            async_sessionmaker(engine, expire_on_commit=False),
            capability_secret=SecretStr("synthetic-consent-test-only"),
            callback_sender=no_callback,
            enabled=True,
        )
        app.state.voice_runtime = demo.runtime
        base = f"/api/workspaces/{world.ctx.tenant_id}"
        setup = world.client.post(base + "/voice-sessions/setup", json={"request_id": str(uuid4())})
        assert setup.status_code == 200, setup.text
        connection_id = setup.json()["connections"][0]["id"]
        invoice = world.store.get(InvoiceSnapshot, world.invoice_id)
        assert invoice
        storage = ProviderConnection(
            tenant_id=world.ctx.tenant_id, provider="s3", provider_type="storage", status="READY"
        )
        world.store.add(storage)
        world.store.commit()
        command = {
            "request_id": str(uuid4()),
            "customer_id": str(invoice.customer_id),
            "voice_connection_id": connection_id,
            "storage_connection_id": str(storage.id),
            "decision": "GRANTED",
            "scopes": {
                "audio_capture": True,
                "transcript": True,
                "private_storage": True,
                "playback": False,
                "provider_retention": True,
            },
            "purpose": "Explicit synthetic privacy test",
            "jurisdiction": "AU-VIC",
            "notice": "Fixed synthetic fixture only; no customer voice is collected.",
            "evidence": "Current Owner explicitly reviewed each independent processing scope.",
            "expires_at": (now() + timedelta(days=1)).isoformat(),
        }
        prefix = base + "/voice-consents"
        denied = world.client.post(prefix, json=command, headers={"x-csrf-token": "invalid"})
        assert denied.status_code == 403, denied.text
        denied = world.client.post(prefix, json=command | {"customer_id": str(uuid4())})
        assert denied.status_code == 403, denied.text
        hidden = world.client.post(prefix, json=command | {"decision": "DECLINED"})
        assert hidden.status_code == 422
        recorded = world.client.post(prefix, json=command)
        assert recorded.status_code == 201, recorded.text
        value = recorded.json()
        assert value["active"] and value["synthetic_only"] and value["retention_days"] == 30
        assert not value["command"]["scopes"]["playback"]
        assert world.client.post(prefix, json=command).json()["id"] == value["id"]
        assert (
            world.client.post(prefix, json=command | {"purpose": "Changed original purpose"}).status_code
            == 409
        )
        world.store.expire_all()
        for sql in ("UPDATE consent_evidence SET material_hash='changed'", "DELETE FROM consent_evidence"):
            with pytest.raises(DBAPIError), world.store.begin_nested():
                world.store.execute(text(sql))
        assert world.store.scalar(select(func.count()).select_from(ConsentEvidence)) == 1
        world.store.rollback()
        prepare = world.client.post(
            base + "/voice-sessions",
            json={
                "request_id": str(uuid4()),
                "connection_id": connection_id,
                "invoice_ids": [str(world.invoice_id)],
                "objective": "Review synthetic permitted artifacts",
                "consent_id": value["id"],
            },
        )
        assert prepare.status_code == 201, prepare.text
        assert prepare.json()["consent"]["material_hash"] == value["material_hash"]
        from aco.features.review.approvals import HumanApprovalService

        proposal = HumanApprovalService(world.store).get(world.ctx, UUID(prepare.json()["approval_id"]))
        assert proposal.proposal.model_dump()["consent_hash"] == value["material_hash"]
        assert proposal.proposal.model_dump()["capture"] == "SYNTHETIC_ARTIFACTS"
        world.store.rollback()
        withdrawal = {
            "request_id": str(uuid4()),
            "expected_version": value["version"],
            "reason": "Withdraw all synthetic capture permission immediately",
        }
        withdrawn = world.client.post(prefix + f"/{value['id']}/revoke", json=withdrawal)
        assert withdrawn.status_code == 200, withdrawn.text
        assert not withdrawn.json()["active"] and "CONSENT_REVOKED" in withdrawn.json()["reasons"]
        assert world.client.post(prefix + f"/{value['id']}/revoke", json=withdrawal).status_code == 200
        world.store.expire_all()
        assert world.store.scalar(select(func.count()).select_from(ConsentRevocation)) == 1
        with pytest.raises(DBAPIError), world.store.begin_nested():
            world.store.execute(text("DELETE FROM consent_revocations"))
        member = world.store.scalar(select(Membership).where(Membership.user_id == world.ctx.actor_id))
        assert member
        member.role = "operator"
        member.version += 1
        world.store.commit()
        assert world.client.post(prefix, json=command | {"request_id": str(uuid4())}).status_code == 403
        page = world.client.get(prefix)
        assert page.status_code == 200 and not page.json()["can_record"]
        assert "CONSENT_AUTHORITY_CHANGED" in page.json()["items"][0]["reasons"]
        await engine.dispose()


def test_boundary_retention_and_legal_review(boundary_observer: BoundaryObserver) -> None:
    import json
    from datetime import datetime, timedelta
    from pathlib import Path
    from uuid import uuid4

    from artifact_support import artifact_world, upload
    from boundary_contracts import boundary_inventory
    from sqlalchemy import select

    from aco.features.artifacts.models import Artifact, ArtifactPurge

    ids = set(boundary_inventory()["retention"]["case_ids"])
    cases = [
        c
        for c in json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]
        if c["id"] in ids
    ]
    seen: set[str] = set()
    with artifact_world() as (client, store, tenant):
        for case in cases:
            seen.add(case["id"])
            given, expected = case["input"], case["expected"]
            if case["id"] == "retention_synthetic_expiry":
                assert given["synthetic"] and not given["verified_legal_hold"]
                saved = upload(client, tenant, b"Explicit retained synthetic fixture").json()
                row = store.get_one(Artifact, saved["id"])
                captured = datetime.fromisoformat(given["captured_at"].replace("Z", "+00:00"))
                instant = datetime.fromisoformat(given["now"].replace("Z", "+00:00"))
                row.captured_at, row.expires_at = captured, captured + timedelta(days=30)
                purge = store.scalar(select(ArtifactPurge).where(ArtifactPurge.artifact_id == row.id))
                assert purge
                purge.due_at = row.expires_at
                store.commit()
                denied = client.get(f"/api/workspaces/{tenant}/artifacts/{row.id}/content")
                assert denied.status_code == 404
                store.expire_all()
                purge = store.get_one(ArtifactPurge, purge.id)
                deadline = row.expires_at + timedelta(hours=24)
                assert row.expires_at == instant and purge.due_at <= deadline
                actual = {
                    "access_allowed": denied.status_code == 200,
                    "retention_days": (row.expires_at - captured).days,
                    "purge_due_at": deadline.isoformat().replace("+00:00", "Z"),
                    "hidden_verbatim_copy_allowed": "body" in Artifact.__table__.columns,
                }
                assert actual == expected
                boundary_observer.assert_observed(str(case["id"]), actual)
                store.rollback()
                assert (
                    client.post(f"/api/workspaces/{tenant}/artifacts/{saved['id']}/purge").status_code == 200
                )
            elif case["id"] == "retention_real_policy_missing_blocks":
                assert not given["synthetic"] and given["approved_retention_policy"] is None
                response = client.post(
                    f"/api/workspaces/{tenant}/artifacts",
                    json={
                        "source_kind": "provider_call",
                        "source_reference": str(uuid4()),
                        "content_type": "application/json",
                        "content_base64": "e30=",
                        "capture_class": "real",
                    },
                )
                assert response.status_code == 403
                actual = {
                    "capture_or_ingest_allowed": response.status_code == 201,
                    "status": "BLOCKED" if response.status_code == 403 else "UNEXPECTED",
                }
                assert actual == expected
                boundary_observer.assert_observed(str(case["id"]), actual)
            else:
                assert given["legal_hold_claimed"] and not given["authority_verified"]
                saved = upload(client, tenant, b"Synthetic legal review fixture").json()
                prefix = f"/api/workspaces/{tenant}/artifacts/{saved['id']}"
                privacy = client.get(prefix + "/privacy").json()
                command = {
                    "request_id": str(uuid4()),
                    "expected_version": privacy["version"],
                    "kind": "QUARANTINE",
                    "reason": "Claimed legal hold requires actual authority review",
                }
                held = client.post(prefix + "/privacy", json=command)
                assert held.status_code == 200, held.text
                blocked_read = client.get(prefix + "/content")
                blocked_purge = client.post(prefix + "/purge")
                assert blocked_read.status_code == 404
                assert blocked_purge.status_code == 409
                recapture = upload(
                    client, tenant, b"Synthetic legal review fixture", source=saved["source_reference"]
                )
                assert recapture.status_code == 410
                actual = {
                    "app_access_allowed": blocked_read.status_code == 200,
                    "new_capture_allowed": recapture.status_code == 201,
                    "destructive_purge_allowed": blocked_purge.status_code == 200,
                    "review_required": held.json()["legal_hold_state"] == "review_required",
                }
                assert actual == expected
                boundary_observer.assert_observed(str(case["id"]), actual)
                from aco.features.identity.models import now

                privacy = client.get(prefix + "/privacy").json()
                released = client.post(
                    prefix + "/privacy",
                    json={
                        "request_id": str(uuid4()),
                        "expected_version": privacy["version"],
                        "kind": "RELEASE_HOLD",
                        "reason": "Close the explicitly synthetic legal-review test fixture",
                        "authority": "Current Owner disposition of a synthetic test artifact",
                        "evidence": "No customer material or real legal process in this fixture",
                        "review_at": (now() + timedelta(days=1)).isoformat(),
                    },
                )
                assert released.status_code == 200, released.text
                assert client.post(prefix + "/purge").status_code == 200
    assert seen == ids


async def test_same_tenant_read_revocation_boundary_cases(boundary_observer: BoundaryObserver) -> None:
    import json
    from pathlib import Path
    from uuid import uuid4

    from accounting_support import context
    from artifact_support import artifact_world, storage_runtime, upload
    from boundary_contracts import boundary_inventory
    from sqlalchemy import select

    from aco.features.artifacts.models import Artifact, ArtifactPurge
    from aco.features.artifacts.service import object_ref

    ids = set(boundary_inventory()["artifact_revocation"]["case_ids"])
    cases = [
        c
        for c in json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]
        if c["id"] in ids
    ]
    seen: set[str] = set()
    with artifact_world() as (client, store, tenant):
        for case in cases:
            seen.add(case["id"])
            given, expected = case["input"], case["expected"]
            assert given["owner_tenant"] == given["request_tenant"] == "T1"
            assert given["authorized_user"] == "U1" and given["role"] == "owner"
            saved = upload(client, tenant, b"Synthetic immediate revocation fixture").json()
            row = store.get_one(Artifact, saved["id"])
            prefix = f"/api/workspaces/{tenant}/artifacts/{row.id}"
            from aco.features.identity.models import Membership

            member = store.scalar(select(Membership).where(Membership.tenant_id == tenant))
            assert member is not None
            ctx = context(tenant, member.user_id)
            port = storage_runtime(client).bind(tenant, row.provider_connection_id)
            ref = object_ref(row)
            signed = await port.get_signed_read_url(ctx, ref, 60)
            assert signed.value and given["internal_signed_url_not_expired"]
            assert (client.get(prefix + "/content").status_code == 200) == given[
                "initial_authorization_allowed"
            ]
            if given["after_authorization_event"] == "consent_revoked":
                # The legacy artifact revocation flag is itself the persisted privacy boundary;
                # actual voice ConsentEvidence withdrawal is exercised by test_voice_postcall.
                row.consent_revoked = True
                store.commit()
            else:
                store.rollback()
                privacy = client.get(prefix + "/privacy").json()
                requested = client.post(
                    prefix + "/privacy",
                    json={
                        "request_id": str(uuid4()),
                        "expected_version": privacy["version"],
                        "kind": "DELETE_REQUEST",
                        "reason": "Explicit privacy deletion request pending Owner disposition",
                    },
                )
                assert requested.status_code == 200, requested.text
            denied = client.get(prefix + "/content")
            metadata = await port.get_metadata(ctx, ref)
            store.expire_all()
            purge = store.scalar(select(ArtifactPurge).where(ArtifactPurge.artifact_id == row.id))
            assert purge and (purge.status == "deleted") == given["purge_complete"]
            actual = {
                "next_proxy_read_allowed": denied.status_code == 200,
                "http_status": denied.status_code,
                "raw_object_may_still_exist_pending_purge": metadata.value.sha256 == row.sha256,
                "hidden_verbatim_copy_allowed": "body" in Artifact.__table__.columns,
            }
            assert actual == expected
            boundary_observer.assert_observed(str(case["id"]), actual)
            store.rollback()
            # Exact owned object cleanup is an explicit current Owner decision.
            assert client.post(prefix + "/purge").status_code == 200
    assert seen == ids


def test_finance_privacy_request_requires_current_owner_for_destruction() -> None:
    from uuid import uuid4

    import pytest
    from artifact_support import artifact_world, upload
    from sqlalchemy import select, text
    from sqlalchemy.exc import DBAPIError

    from aco.features.identity.models import Membership

    with artifact_world() as (client, store, tenant):
        saved = upload(
            client, tenant, b"Synthetic privacy request requiring current Owner disposition"
        ).json()
        prefix = f"/api/workspaces/{tenant}/artifacts/{saved['id']}"
        member = store.scalar(select(Membership).where(Membership.tenant_id == tenant))
        assert member
        member.role, member.version = "finance_manager", member.version + 1
        store.commit()
        before = client.get(prefix + "/privacy").json()
        command = {
            "request_id": str(uuid4()),
            "expected_version": before["version"],
            "kind": "DELETE_REQUEST",
            "reason": "Finance requests deletion of a synthetic test artifact",
        }
        requested = client.post(prefix + "/privacy", json=command)
        assert requested.status_code == 200, requested.text
        snapshot = requested.json()
        assert snapshot["can_request_deletion"] and not snapshot["can_review"]
        assert client.get(prefix + "/content").status_code == 404
        decision = {
            "request_id": str(uuid4()),
            "expected_version": snapshot["version"],
            "kind": "DELETE_APPROVED",
            "parent_id": snapshot["events"][0]["id"],
            "reason": "Explicit Owner reviewed artifact-specific deletion request",
        }
        assert client.post(prefix + "/privacy", json=decision).status_code == 403
        assert client.post(prefix + "/purge").status_code == 403
        assert (
            snapshot["purge_status"] == "blocked"
            and snapshot["failure_code"] == "OWNER_DELETION_REVIEW_REQUIRED"
        )
        store.expire_all()
        member = store.get_one(Membership, member.id)
        member.role, member.version = "owner", member.version + 1
        store.commit()
        stale = client.post(prefix + "/privacy", json=decision | {"expected_version": before["version"]})
        assert stale.status_code == 409
        approved = client.post(prefix + "/privacy", json=decision)
        assert approved.status_code == 200 and approved.json()["purge_status"] == "purge_pending", (
            approved.text
        )
        assert client.post(prefix + "/privacy", json=decision).status_code == 200
        assert (
            client.post(
                prefix + "/privacy", json=decision | {"reason": "Changed reviewed request payload"}
            ).status_code
            == 409
        )
        assert client.post(prefix + "/purge").json()["status"] == "deleted"
        with pytest.raises(DBAPIError), store.begin_nested():
            store.execute(text("DELETE FROM artifact_privacy_events"))
