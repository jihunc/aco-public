"""Current human export authority and bounded safe business projections."""

from datetime import timedelta

from accounting_support import seed
from identity_support import actor_client

from aco.features.identity.models import now


def command() -> dict[str, object]:
    return {
        "resources": [
            "customers",
            "invoices",
            "payments",
            "accounting_events",
            "tasks",
            "cases",
            "promises",
            "actions",
            "communications",
            "artifacts",
            "agent_runs",
            "approvals",
            "voice_sessions",
            "usage",
            "policies",
            "audit_history",
        ],
        "created_from": (now() - timedelta(days=1)).isoformat(),
        "created_before": (now() + timedelta(minutes=1)).isoformat(),
    }


def test_current_owner_downloads_fixed_business_export_without_private_configuration() -> None:
    with actor_client("owner") as (client, db, user, tenant):
        seed(client, tenant)
        response = client.post(f"/api/workspaces/{tenant}/exports", json=command())
        assert response.status_code == 200, response.text
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.headers["cache-control"] == "no-store"
        data = response.json()
        assert data["tenant_id"] == str(tenant)
        assert all(
            data["counts"][name] > 0
            for name in (
                "customers",
                "invoices",
                "payments",
                "accounting_events",
                "usage",
                "policies",
                "audit_history",
            )
        )
        selected = command()["resources"]
        assert isinstance(selected, list)
        assert set(data["records"]) == set(selected)
        assert data["raw_originals_included"] is False
        assert "encrypted_credentials_ref" not in response.text
        assert "object_key" not in response.text
        assert "synthetic-secret" not in response.text


def test_export_current_four_roles_two_tenants_revocation_csrf_and_audit() -> None:
    import hashlib
    import json
    from typing import cast
    from uuid import uuid4

    from fastapi import FastAPI
    from identity_support import add_actor
    from sqlalchemy import select

    from aco.features.accounting.models import InvoiceSnapshot
    from aco.features.identity.models import AuditEvent, Membership, ProviderConnection, Tenant

    with actor_client("owner") as (owner, db, user, tenant):
        seed(owner, tenant)
        other = Tenant(name="Other export tenant", timezone="UTC")
        db.add(other)
        db.flush()
        db.add_all(
            [
                Membership(tenant_id=other.id, user_id=user.id, role="owner"),
                ProviderConnection(tenant_id=other.id, provider="demo", provider_type="accounting"),
            ]
        )
        db.commit()
        seed(owner, other.id)
        own_ids = set(db.scalars(select(InvoiceSnapshot.id).where(InvoiceSnapshot.tenant_id == tenant)))
        other_ids = set(db.scalars(select(InvoiceSnapshot.id).where(InvoiceSnapshot.tenant_id == other.id)))
        db.add(
            AuditEvent(
                tenant_id=tenant,
                actor_id=user.id,
                correlation_id=uuid4(),
                action="synthetic.export.canary",
                target_id=uuid4(),
                details={"credential": "private-fixture-canary-never-export"},
            )
        )
        db.commit()
        body = command() | {"resources": ["invoices", "audit_history"]}
        for role in ("owner", "finance_manager", "operator", "viewer"):
            actor, client = add_actor(db, cast(FastAPI, owner.app), f"export-{role}@synthetic.invalid")
            memberships = [
                Membership(tenant_id=key, user_id=actor.id, role=role) for key in (tenant, other.id)
            ]
            db.add_all(memberships)
            db.commit()
            for key, ids, excluded in ((tenant, own_ids, other_ids), (other.id, other_ids, own_ids)):
                result = client.post(f"/api/workspaces/{key}/exports", json=body)
                if role not in {"owner", "finance_manager"}:
                    assert result.status_code == 403, result.text
                    continue
                assert result.status_code == 200, result.text
                assert {row["id"] for row in result.json()["records"]["invoices"]} == {str(v) for v in ids}
                assert all(str(v) not in result.text for v in excluded)
                assert "private-fixture-canary-never-export" not in result.text
                digest = hashlib.sha256(result.content).hexdigest()
                assert result.headers["x-content-sha256"] == digest
                db.rollback()
                audit = db.scalar(
                    select(AuditEvent)
                    .where(
                        AuditEvent.tenant_id == key,
                        AuditEvent.actor_id == actor.id,
                        AuditEvent.action == "tenant.business_export",
                    )
                    .order_by(AuditEvent.created_at.desc())
                )
                assert audit is not None and audit.details["source_sha256"] == digest
                count_json = audit.details["counts"]
                assert isinstance(count_json, str)
                assert json.loads(count_json)["invoices"] == len(ids)
                assert "records" not in audit.details
                db.rollback()
            if role in {"owner", "finance_manager"}:
                token = client.headers.pop("x-csrf-token")
                assert client.post(f"/api/workspaces/{tenant}/exports", json=body).status_code == 403
                client.headers["x-csrf-token"] = token
            memberships[1].active = False
            memberships[1].version += 1
            db.commit()
            assert (
                client.post(
                    f"/api/workspaces/{other.id}/exports",
                    json=body,
                    headers={"x-role": "owner", "x-tenant-id": str(tenant)},
                ).status_code
                == 403
            )
            client.close()


def test_export_limits_window_and_fixed_selection_fail_without_partial_file_or_audit(
    monkeypatch: object,
) -> None:
    import pytest
    from sqlalchemy import func, select

    from aco.features.exports import service
    from aco.features.identity.models import AuditEvent

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    with actor_client("owner") as (client, db, user, tenant):
        seed(client, tenant)
        path = f"/api/workspaces/{tenant}/exports"
        body = command() | {"resources": ["invoices"]}
        prior = db.scalar(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "tenant.business_export")
        )
        db.rollback()
        invalid_commands: tuple[dict[str, object], ...] = (
            {"resources": []},
            {"resources": ["credentials"]},
            {"resources": ["invoices", "invoices"]},
            {"tenant_id": str(tenant)},
            {"created_from": "2026-01-01T00:00:00"},
            {"created_from": (now() - timedelta(days=367)).isoformat()},
            {"created_before": (now() - timedelta(days=2)).isoformat()},
        )
        for invalid in invalid_commands:
            assert client.post(path, json=body | invalid).status_code == 422
        for field, limit, reason in (("MAX_RECORDS", 1, "ROW"), ("MAX_BYTES", 64, "BYTE")):
            with monkeypatch.context() as scoped:
                scoped.setattr(service, field, limit)
                refused = client.post(path, json=body)
                assert refused.status_code == 413 and reason in refused.json()["detail"]
                assert "content-disposition" not in refused.headers and "records" not in refused.json()
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "tenant.business_export")
            )
            == prior
        )
        db.rollback()
        empty = client.post(
            path,
            json=body
            | {
                "created_from": (now() - timedelta(days=5)).isoformat(),
                "created_before": (now() - timedelta(days=4)).isoformat(),
            },
        )
        assert empty.status_code == 200 and empty.json()["records"]["invoices"] == []


def test_case_promise_and_communication_history_exports_without_private_evidence() -> None:
    from datetime import date

    from accounting_support import context
    from case_promise_support import communication, invoices

    from aco.core.types import Money
    from aco.features.cases.dto import CommunicationEvidence, UpsertCase
    from aco.features.cases.service import CaseService
    from aco.features.promises.dto import RecordPromise
    from aco.features.promises.service import PromiseService

    with actor_client("owner") as (client, db, user, tenant):
        rows = invoices(client, db, tenant)[:1]
        body = "Private source fixture: invoice rate wrong; promise AUD 100 on 2026-11-20."
        msg = communication(db, tenant, rows, body)
        evidence = CommunicationEvidence(communication_id=msg.id, excerpt=body, offset=0)
        ctx = context(tenant, user.id)
        case = CaseService(db).upsert_case(
            ctx,
            UpsertCase(
                customer_id=rows[0].customer_id,
                issue_type="wrong_amount",
                claim_key="fixture-rate",
                substance="Rate dispute",
                invoice_ids=[rows[0].id],
                evidence=evidence,
            ),
        )
        promise = PromiseService(db).record_promise(
            ctx,
            RecordPromise(
                customer_id=rows[0].customer_id,
                invoice_ids=[rows[0].id],
                evidence=evidence,
                amount=Money(minor_units=10000, currency="AUD", exponent=2),
                due_date=date(2026, 11, 20),
                timezone="Australia/Melbourne",
            ),
        )
        case_id, promise_id = str(case.case.id), str(promise.id)
        db.commit()
        response = client.post(
            f"/api/workspaces/{tenant}/exports",
            json=command(),
        )
        assert response.status_code == 200, response.text
        records = response.json()["records"]
        assert records["cases"][0]["id"] == case_id and records["cases"][0]["history"]
        assert records["promises"][0]["id"] == promise_id and records["promises"][0]["history"]
        assert records["promises"][0]["amount"]["minor_units"] == "10000"
        assert records["promises"][0]["installments"]
        assert records["communications"][0]["invoice_links"]
        assert records["tasks"] and body not in response.text
        assert "evidence_excerpt" not in response.text and "text_body" not in response.text


def test_artifact_export_keeps_private_bytes_and_expired_access_outside_manifest() -> None:
    from uuid import UUID

    from artifact_support import artifact_world, upload

    from aco.features.artifacts.models import Artifact

    with artifact_world() as (client, db, tenant):
        response = upload(client, tenant, b"private original export canary")
        assert response.status_code == 201, response.text
        key = UUID(response.json()["id"])
        artifact = db.get(Artifact, key)
        assert artifact is not None
        object_key = artifact.object_key
        db.rollback()
        path = f"/api/workspaces/{tenant}/exports"
        data = command() | {"resources": ["artifacts"]}
        result = client.post(path, json=data)
        assert result.status_code == 200, result.text
        row = result.json()["records"]["artifacts"][0]
        assert row["privacy_read_allowed"] is True
        assert object_key not in result.text and "private original export canary" not in result.text
        artifact = db.get(Artifact, key)
        assert artifact is not None
        artifact.expires_at = now() - timedelta(seconds=1)
        db.commit()
        result = client.post(path, json=data)
        assert result.status_code == 200
        assert result.json()["records"]["artifacts"][0]["privacy_read_allowed"] is False
        db.rollback()
        deleted = client.request(
            "DELETE",
            f"/api/workspaces/{tenant}/artifacts/{key}",
            json={"reason": "Synthetic export fixture cleanup"},
        )
        assert deleted.status_code == 200, deleted.text
        assert client.post(f"/api/workspaces/{tenant}/artifacts/{key}/purge").json()["status"] == "deleted"


def test_maintained_fake_run_exports_actual_steps_tasks_actions_and_source_amounts() -> None:
    import time
    from uuid import uuid4

    from action_support import action_world, workers
    from sqlalchemy import select

    from aco.features.accounting.models import InvoiceSnapshot

    with action_world() as world:
        path = f"/api/workspaces/{world.ctx.tenant_id}"
        before = [
            (r.id, r.balance_minor, r.source_revision) for r in world.store.scalars(select(InvoiceSnapshot))
        ]
        world.store.rollback()
        response = world.client.post(
            path + "/agent-runs",
            json={
                "invoice_ids": [str(world.invoice_id)],
                "objective": "Review synthetic invoice evidence for human verification",
                "request_id": str(uuid4()),
            },
        )
        assert response.status_code == 202, response.text
        run = response.json()
        with workers(world, crash="domain"):
            deadline = time.monotonic() + 25
            while run["status"] in {"queued", "running"}:
                assert time.monotonic() < deadline
                time.sleep(0.1)
                run = world.client.get(path + "/agent-runs/" + run["id"]).json()
        assert run["status"] == "completed", run
        result = world.client.post(
            path + "/exports", json=command() | {"resources": ["agent_runs", "tasks", "actions"]}
        )
        assert result.status_code == 200, result.text
        records = result.json()["records"]
        assert records["agent_runs"][0]["id"] == run["id"]
        assert records["agent_runs"][0]["authenticity"] == "FAKE"
        assert records["agent_runs"][0]["steps"]
        assert records["tasks"] and records["actions"][0]["executions"]
        assert "lease_token" not in result.text and "command" not in result.text
        world.store.rollback()
        assert before == [
            (r.id, r.balance_minor, r.source_revision) for r in world.store.scalars(select(InvoiceSnapshot))
        ]


def test_voice_prepare_exports_actual_session_scope_and_independent_decision() -> None:
    import asyncio
    from uuid import uuid4

    import httpx
    from action_support import action_world
    from fastapi import FastAPI
    from identity_support import add_actor
    from pydantic import SecretStr
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from aco.core.types import Role
    from aco.features.identity.models import Membership, ProviderConnection
    from aco.features.review.approval_dto import ApprovalDecisionCommand
    from aco.features.review.approvals import HumanApprovalService
    from aco.features.voice.dto import PrepareVoice
    from aco.features.voice.runtime import VoiceRuntime
    from aco.features.voice.service import VoiceService
    from aco.integrations.vapi.settings import VapiBinding

    with action_world() as world:
        app = world.client.app
        assert isinstance(app, FastAPI)
        reviewer, reviewer_client = add_actor(world.store, app, "export-reviewer@synthetic.invalid")
        world.store.add(
            Membership(tenant_id=world.ctx.tenant_id, user_id=reviewer.id, role="finance_manager")
        )
        connection = ProviderConnection(
            tenant_id=world.ctx.tenant_id,
            provider="vapi",
            provider_type="voice",
            status="READY",
            encrypted_credentials_ref="export-offline-voice",
        )
        world.store.add(connection)
        world.store.commit()
        binding = VapiBinding(
            tenant_id=world.ctx.tenant_id,
            connection_id=connection.id,
            assistant_id=uuid4(),
            api_key=SecretStr("synthetic-unused"),
            webhook_secret=SecretStr("synthetic-unused-signing"),
            credential_revision="export-offline-voice",
            evidence_mode="MOCKED_HTTP",
        )
        engine = create_async_engine(world.url)
        runtime = VoiceRuntime(
            async_sessionmaker(engine, expire_on_commit=False),
            lambda tenant, conn: binding,
            transport_factory=lambda: httpx.MockTransport(lambda request: httpx.Response(500)),
            capability_secret=SecretStr("synthetic-unused-capability"),
            enabled=True,
        )
        prepared = VoiceService(world.store, runtime).prepare_call(
            world.ctx,
            PrepareVoice(
                request_id=uuid4(),
                connection_id=connection.id,
                invoice_ids=[world.invoice_id],
                objective="Synthetic offline preparation",
            ),
        )
        world.store.commit()
        ctx = world.ctx.model_copy(update={"actor_id": reviewer.id, "role": Role.FINANCE_MANAGER})
        approvals = HumanApprovalService(world.store)
        detail = approvals.get(ctx, prepared.approval_id)
        approvals.decide_approval(
            ctx,
            prepared.approval_id,
            ApprovalDecisionCommand(
                expected_version=detail.version,
                reviewed_action_version=detail.action_version,
                reviewed_material_hash=detail.material_hash,
                reviewed_binding_hash=detail.current_binding_hash,
                decision="denied",
                reason="Export evidence only; no call",
            ),
        )
        world.store.commit()
        result = world.client.post(
            f"/api/workspaces/{world.ctx.tenant_id}/exports",
            json=command() | {"resources": ["voice_sessions", "approvals"]},
        )
        assert result.status_code == 200, result.text
        records = result.json()["records"]
        assert records["voice_sessions"][0]["id"] == str(prepared.id)
        assert records["voice_sessions"][0]["invoice_links"][0]["invoice_id"] == str(world.invoice_id)
        assert records["approvals"][0]["decisions"][0]["decision"] == "denied"
        assert "binding_hash" not in result.text and "synthetic-unused" not in result.text
        assert "command" not in result.text and "proposal" not in result.text
        reviewer_client.close()
        asyncio.run(engine.dispose())
