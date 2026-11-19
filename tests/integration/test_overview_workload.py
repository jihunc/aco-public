"""Actual company business records drive current dashboard counts, never accounting effects."""

from accounting_support import context
from case_promise_support import communication, invoices
from fastapi import FastAPI
from identity_support import actor_client, add_actor
from sqlalchemy import select

from aco.features.cases.dto import CommunicationEvidence, UpsertCase
from aco.features.cases.service import CaseService
from aco.features.identity.models import Membership, ProviderConnection, Tenant
from aco.features.promises.dto import RecordPromise
from aco.features.promises.service import PromiseService
from aco.features.review.dto import TaskCreate, TaskPatch
from aco.features.review.tasks import HumanTaskService


def test_current_workload_counts_real_records_and_rechecks_all_roles_and_tenants() -> None:
    with actor_client("owner") as (client, db, user, tenant):
        rows = invoices(client, db, tenant)
        before = [(r.id, r.balance_minor, r.financial_status, r.source_revision) for r in rows]
        path = f"/api/workspaces/{tenant}/overview/workload"
        initial = client.get(path)
        assert initial.status_code == 200, initial.text
        assert initial.json()["open_tasks"] == 0
        source = communication(
            db, tenant, rows[:1], "Synthetic disputed invoice and unresolved promise terms"
        )
        source_body = source.content["text_body"]
        assert isinstance(source_body, str)
        evidence = CommunicationEvidence(communication_id=source.id, offset=0, excerpt=source_body)
        ctx = context(tenant, user.id)
        CaseService(db).upsert_case(
            ctx,
            UpsertCase(
                customer_id=rows[0].customer_id,
                invoice_ids=[rows[0].id],
                evidence=evidence,
                issue_type="wrong_amount",
                claim_key="dashboard",
                substance="Actual dispute",
            ),
        )
        PromiseService(db).record_promise(
            ctx, RecordPromise(customer_id=rows[0].customer_id, invoice_ids=[rows[0].id], evidence=evidence)
        )
        tasks = HumanTaskService(db)
        task = tasks.create(ctx, TaskCreate(title="Investigate retained dispute"))
        done = tasks.create(ctx, TaskCreate(title="Already handled"))
        tasks.update(ctx, done.id, TaskPatch(expected_version=done.version, status="done"))
        deleted = tasks.create(ctx, TaskCreate(title="Retained deleted task"))
        tasks.soft_delete(ctx, deleted.id, deleted.version)
        db.commit()
        app = client.app
        assert isinstance(app, FastAPI)
        other_user, other_client = add_actor(db, app, "other-company@example.test")
        other = Tenant(name="Different company", timezone="UTC")
        db.add(other)
        db.flush()
        db.add(Membership(tenant_id=other.id, user_id=other_user.id, role="owner"))
        db.add(
            ProviderConnection(
                tenant_id=other.id,
                provider_type="accounting",
                provider="other",
                status="AUTH_EXPIRED",
                encrypted_credentials_ref="NEVER_EXPORT_OR_DISPLAY_THIS",
            )
        )
        db.commit()
        HumanTaskService(db).create(context(other.id, other_user.id), TaskCreate(title="Other company task"))
        db.commit()
        clients = [client]
        for role in ["finance_manager", "operator", "viewer"]:
            member_user, member_client = add_actor(db, app, f"{role}@example.test")
            db.add(Membership(tenant_id=tenant, user_id=member_user.id, role=role))
            db.commit()
            clients.append(member_client)
        for current in clients:
            response = current.get(path)
            assert response.status_code == 200, response.text
            data = response.json()
            assert data["open_tasks"] == 3  # One manual, one dispute review, one promise confirmation.
            assert data["unresolved_cases"] == 1
            assert data["unconfirmed_promises"] == 1
            assert data["active_promises"] == 0 and data["completed_investigations"] == 0
            assert data["pending_approvals"] == 0 and data["active_investigations"] == 0
            assert data["outbound_paused"] is True and data["automation_enabled"] is False
            assert (
                len(data["accounting_sources"]) == 1 and data["accounting_sources"][0]["provider"] == "demo"
            )
            assert "NEVER_EXPORT_OR_DISPLAY_THIS" not in response.text and str(other.id) not in response.text
            assert current.get(f"/api/workspaces/{other.id}/overview/workload").status_code == 403
        foreign = other_client.get(f"/api/workspaces/{other.id}/overview/workload")
        assert foreign.status_code == 200 and foreign.json()["open_tasks"] == 1
        assert foreign.json()["unresolved_cases"] == 0 and foreign.json()["unconfirmed_promises"] == 0
        assert client.get(path, params={"tenant_id": str(other.id)}).status_code == 422
        tasks.update(ctx, task.id, TaskPatch(expected_version=task.version, status="done"))
        db.commit()
        assert client.get(path).json()["open_tasks"] == 2
        membership = db.scalar(
            select(Membership).where(Membership.tenant_id == tenant, Membership.user_id == user.id)
        )
        assert membership is not None
        membership.active = False
        db.commit()
        assert client.get(path).status_code == 403
        db.expire_all()
        assert [(r.id, r.balance_minor, r.financial_status, r.source_revision) for r in rows] == before


def test_invoice_scoped_worker_cannot_expand_into_company_workload() -> None:
    from typing import cast

    import pytest
    from action_support import action_world, enqueue
    from fastapi import HTTPException

    from aco.core.context import TenantContext
    from aco.features.actions.models import ScheduledAction
    from aco.features.actions.service import worker_context
    from aco.features.identity.service import IdentityService, Permission
    from aco.features.receivables.workload import current_workload

    with action_world() as world:
        ref = enqueue(world)
        action = world.store.get(ScheduledAction, ref.action_id)
        assert action is not None
        ctx = worker_context(action)
        IdentityService(world.store).authorize(ctx, Permission.INVOICE_READ)
        with pytest.raises(HTTPException) as denied:
            current_workload(world.store, cast(TenantContext, ctx))
        assert denied.value.status_code == 403 and denied.value.detail == "HUMAN_REQUIRED"
