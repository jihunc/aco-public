"""Invoice activity uses real domain audit and current human scope, never raw audit details."""

import pytest
from accounting_support import context, seed
from case_promise_support import communication, invoices
from fastapi import FastAPI, HTTPException
from identity_support import actor_client, add_actor
from sqlalchemy import select

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.cases.dto import CommunicationEvidence, UpsertCase
from aco.features.cases.service import CaseService
from aco.features.identity.models import AuditEvent, Membership, ProviderConnection, Tenant
from aco.features.identity.service import IdentityService
from aco.features.promises.dto import RecordPromise
from aco.features.promises.service import PromiseService
from aco.features.review.dto import TaskCreate, TaskPatch
from aco.features.review.tasks import HumanTaskService


def test_invoice_audit_reads_actual_task_case_promise_history_without_private_payloads() -> None:
    with actor_client("owner") as (client, db, user, tenant):
        rows = invoices(client, db, tenant)
        invoice = rows[0]
        ctx = context(tenant, user.id)
        tasks = HumanTaskService(db)
        task = tasks.create(ctx, TaskCreate(title="Synthetic related task", invoice_id=invoice.id))
        task = tasks.update(ctx, task.id, TaskPatch(expected_version=task.version, status="done"))
        deleted = tasks.create(ctx, TaskCreate(title="Retained deleted task", invoice_id=invoice.id))
        tasks.soft_delete(ctx, deleted.id, deleted.version)
        unrelated = tasks.create(ctx, TaskCreate(title="Company task without invoice"))
        msg = communication(db, tenant, rows[:2], "Explicit synthetic PO issue and unresolved commitment")
        body = msg.content["text_body"]
        assert isinstance(body, str)
        evidence = CommunicationEvidence(communication_id=msg.id, offset=0, excerpt=body)
        case = CaseService(db).upsert_case(
            ctx,
            UpsertCase(
                customer_id=invoice.customer_id,
                invoice_ids=[r.id for r in rows[:2]],
                evidence=evidence,
                issue_type="wrong_po",
                claim_key="invoice-activity",
                substance="Synthetic PO dispute",
            ),
        )
        promise = PromiseService(db).record_promise(
            ctx,
            RecordPromise(
                customer_id=invoice.customer_id,
                invoice_ids=[invoice.id],
                evidence=evidence,
            ),
        )
        private_marker = "PRIVATE_SYNTHETIC_AUDIT_DETAILS_MUST_NOT_LEAVE"
        IdentityService(db).audit(ctx, "task.updated", task.id, {"private": private_marker})
        unknown = IdentityService(db).audit(ctx, "workspace.updated", invoice.id, {"private": private_marker})
        db.commit()
        financial_before = [(r.id, r.balance_minor, r.financial_status, r.source_revision) for r in rows]
        count_before = len(list(db.scalars(select(AuditEvent.id))))
        response = client.get(f"/api/workspaces/{tenant}/receivables/{invoice.id}/audit")
        assert response.status_code == 200, response.text
        detail = client.get(f"/api/workspaces/{tenant}/receivables/{invoice.id}")
        assert detail.status_code == 200
        assert detail.json()["sections"]["audit"] == "available"
        data = response.json()
        assert data["invoice_id"] == str(invoice.id)
        items = data["items"]
        targets = {row["target_id"] for row in items}
        assert {str(task.id), str(deleted.id), str(case.case.id), str(promise.id)} <= targets
        assert str(unrelated.id) not in targets
        assert str(unknown.id) not in {row["id"] for row in items}
        assert private_marker not in response.text and '"details"' not in response.text
        assert user.email not in response.text
        deleted_rows = [row for row in items if row["target_id"] == str(deleted.id)]
        assert deleted_rows and all(not row["target_available"] for row in deleted_rows)
        assert all(row["actor_kind"] in {"human", "service"} for row in items)
        task_rows = [row for row in items if row["target_id"] == str(task.id)]
        assert {row["action"] for row in task_rows} >= {"task.created", "task.updated"}
        assert all(row["actor_id"] == str(user.id) for row in task_rows)
        assert all(row["correlation_id"] == str(ctx.correlation_id) for row in task_rows)
        actual_ids = {str(key) for key in db.scalars(select(AuditEvent.id))}
        assert {row["id"] for row in items} <= actual_ids
        second = client.get(f"/api/workspaces/{tenant}/receivables/{rows[1].id}/audit").json()
        assert str(case.case.id) in {row["target_id"] for row in second["items"]}
        assert str(promise.id) not in {row["target_id"] for row in second["items"]}
        task_list = client.get(f"/api/workspaces/{tenant}/tasks", params={"invoice_id": str(invoice.id)})
        assert task_list.status_code == 200
        assert str(task.id) in {row["id"] for row in task_list.json()["items"]}
        assert str(deleted.id) not in {row["id"] for row in task_list.json()["items"]}
        db.expire_all()
        assert [
            (r.id, r.balance_minor, r.financial_status, r.source_revision) for r in rows
        ] == financial_before
        assert len(list(db.scalars(select(AuditEvent.id)))) == count_before


def test_invoice_audit_current_viewer_foreign_ids_cursor_and_revocation() -> None:
    with actor_client("owner") as (client, db, user, tenant):
        rows = invoices(client, db, tenant)
        invoice = rows[0]
        ctx = context(tenant, user.id)
        tasks = HumanTaskService(db)
        task = tasks.create(ctx, TaskCreate(title="Bounded audit pagination", invoice_id=invoice.id))
        for index in range(5):
            task = tasks.update(ctx, task.id, TaskPatch(expected_version=task.version, notes=str(index)))
        other = Tenant(name="Separate synthetic invoice company", timezone="UTC")
        db.add(other)
        db.flush()
        db.add(Membership(tenant_id=other.id, user_id=user.id, role="owner"))
        db.add(ProviderConnection(tenant_id=other.id, provider="demo", provider_type="accounting"))
        db.commit()
        seed(client, other.id)
        foreign = db.scalar(select(InvoiceSnapshot).where(InvoiceSnapshot.tenant_id == other.id))
        assert foreign is not None
        foreign_event = db.scalar(select(AuditEvent).where(AuditEvent.tenant_id == other.id))
        assert foreign_event is not None
        app = client.app
        assert isinstance(app, FastAPI)
        viewer, viewer_client = add_actor(db, app, "invoice-audit-viewer@example.test")
        member = Membership(tenant_id=tenant, user_id=viewer.id, role="viewer")
        db.add(member)
        db.commit()
        path = f"/api/workspaces/{tenant}/receivables/{invoice.id}/audit"
        assert viewer_client.get(path).status_code == 200
        all_ids: list[str] = []
        cursor = None
        for _ in range(20):
            params = {"limit": "2"}
            if cursor:
                params["before_id"] = cursor
            page = viewer_client.get(path, params=params)
            assert page.status_code == 200, page.text
            data = page.json()
            assert len(data["items"]) <= 2
            all_ids.extend(row["id"] for row in data["items"])
            cursor = data["next_before_id"]
            if cursor is None:
                break
        assert cursor is None and len(all_ids) >= 6 and len(all_ids) == len(set(all_ids))
        assert all_ids == [row["id"] for row in viewer_client.get(path).json()["items"]]
        assert viewer_client.get(path, params={"limit": 101}).status_code == 422
        assert viewer_client.get(path, params={"role": "owner"}).status_code == 422
        assert viewer_client.get(path, params={"before_id": str(foreign_event.id)}).status_code == 404
        assert (
            viewer_client.get(f"/api/workspaces/{tenant}/receivables/{foreign.id}/audit").status_code == 404
        )
        assert (
            viewer_client.get(f"/api/workspaces/{other.id}/receivables/{foreign.id}/audit").status_code == 403
        )
        member.active = False
        db.commit()
        assert viewer_client.get(path).status_code == 403


def test_invoice_scoped_worker_cannot_read_human_invoice_audit() -> None:
    from action_support import action_world, enqueue

    from aco.features.actions.models import ScheduledAction
    from aco.features.actions.service import worker_context
    from aco.features.identity.service import Permission
    from aco.features.receivables.activity import InvoiceAuditQuery, invoice_audit

    with action_world() as world:
        ref = enqueue(world)
        action = world.store.get(ScheduledAction, ref.action_id)
        assert action is not None
        ctx = worker_context(action)
        IdentityService(world.store).authorize(ctx, Permission.INVOICE_READ)
        with pytest.raises(HTTPException) as denied:
            invoice_audit(world.store, ctx, action.invoice_id, InvoiceAuditQuery())
        assert denied.value.status_code == 403 and denied.value.detail == "HUMAN_REQUIRED"
