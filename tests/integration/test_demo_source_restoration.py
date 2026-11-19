"""Public reset restores actual synthetic source facts without replacing current identities."""

from uuid import uuid4

from action_support import action_world, run_attempts
from integration.test_demo_reset_safety import another_action
from sqlalchemy import select, text

from aco.features.accounting.demo_api import router
from aco.features.accounting.models import (
    Customer,
    DemoSourceRecord,
    InvoiceSnapshot,
    VerifiedPaymentAllocation,
)
from aco.features.actions.service import ScheduledActionRef
from aco.features.identity.models import Tenant


def test_partial_full_source_reset_replay_and_second_journey_preserve_unknown() -> None:
    with action_world() as world:
        world.client.app.include_router(router)
        unknown_id = another_action(world, 1)
        unknown = world.action(unknown_id)
        run_attempts(
            world,
            ScheduledActionRef(action_id=unknown_id, version=unknown.version),
            "UNKNOWN_SIDE_EFFECT",
            attempts=1,
        )
        unknown_before = world.store.scalar(
            text("SELECT to_jsonb(a) FROM scheduled_actions a WHERE id=:id"), {"id": unknown_id}
        )
        before_ids = list(
            world.store.scalars(
                select(InvoiceSnapshot.id)
                .where(InvoiceSnapshot.tenant_id == world.ctx.tenant_id)
                .order_by(InvoiceSnapshot.id)
            )
        )
        customers = list(
            world.store.scalars(
                select(Customer.id).where(Customer.tenant_id == world.ctx.tenant_id).order_by(Customer.id)
            )
        )
        original_sources = world.store.execute(text("SELECT id,to_jsonb(s) FROM demo_source_records s")).all()
        world.store.rollback()
        payment_url = f"/api/workspaces/{world.ctx.tenant_id}/demo/accounting/simulate-payment"
        for amount, expected in ((2500, "partially_paid"), (7500, "paid")):
            response = world.client.post(
                payment_url,
                json={
                    "provider_connection_id": str(world.connection_id),
                    "operation_id": str(uuid4()),
                    "external_invoice_id": "INV-101",
                    "amount": {"minor_units": amount, "currency": "AUD", "exponent": 2},
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["financial_status"] == expected
        world.store.rollback()
        tenant = world.store.get(Tenant, world.ctx.tenant_id)
        assert tenant is not None
        command = {
            "request_id": str(uuid4()),
            "expected_generation": tenant.version,
            "evidence": "Owner reviewed synthetic source restoration and retained history",
        }
        world.store.rollback()
        url = f"/api/workspaces/{world.ctx.tenant_id}/demo/reset"
        response = world.client.post(url, json=command)
        assert response.status_code == 200, response.text
        saved = response.json()
        assert saved["source_restoration"] == "RESTORED"
        assert set(saved["restored_invoice_ids"]) == {str(key) for key in before_ids}
        assert set(saved["restored_customer_ids"]) == {str(key) for key in customers}
        assert len(saved["reversal_allocation_ids"]) == 2
        world.store.rollback()
        invoice = world.store.get(InvoiceSnapshot, world.invoice_id, populate_existing=True)
        assert invoice is not None
        assert (invoice.balance_minor, invoice.financial_status) == (10000, "open")
        assert (
            list(
                world.store.scalars(
                    select(InvoiceSnapshot.id)
                    .where(InvoiceSnapshot.tenant_id == world.ctx.tenant_id)
                    .order_by(InvoiceSnapshot.id)
                )
            )
            == before_ids
        )
        rows = list(
            world.store.scalars(
                select(VerifiedPaymentAllocation).where(VerifiedPaymentAllocation.invoice_id == invoice.id)
            )
        )
        positives = {row.id: row for row in rows if row.kind == "cash"}
        reversals = [row for row in rows if row.kind == "reversal"]
        assert len(positives) == len(reversals) == 2
        assert sum(row.minor_units for row in reversals) == -10000
        assert all(
            row.parent_id in positives and row.minor_units == -positives[row.parent_id].minor_units
            for row in reversals
        )
        assert (
            world.store.scalar(
                text("SELECT to_jsonb(a) FROM scheduled_actions a WHERE id=:id"), {"id": unknown_id}
            )
            == unknown_before
        )
        for source_id, payload in original_sources:
            assert (
                world.store.scalar(
                    text("SELECT to_jsonb(s) FROM demo_source_records s WHERE id=:id"), {"id": source_id}
                )
                == payload
            )
        source_count = len(list(world.store.scalars(select(DemoSourceRecord.id))))
        world.store.rollback()
        assert world.client.post(url, json=command).json() == saved
        world.store.rollback()
        assert len(list(world.store.scalars(select(DemoSourceRecord.id)))) == source_count
        world.store.rollback()
        response = world.client.post(
            payment_url,
            json={
                "provider_connection_id": str(world.connection_id),
                "operation_id": str(uuid4()),
                "external_invoice_id": "INV-101",
                "amount": {"minor_units": 2500, "currency": "AUD", "exponent": 2},
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["financial_status"] == "partially_paid"
        second = world.client.post(
            url, json={**command, "request_id": str(uuid4()), "expected_generation": saved["generation"]}
        )
        assert second.status_code == 200, second.text
        assert second.json()["source_restoration"] == "RESTORED"
        assert len(second.json()["reversal_allocation_ids"]) == 1
        world.store.rollback()
        invoice = world.store.get(InvoiceSnapshot, world.invoice_id, populate_existing=True)
        assert invoice is not None and invoice.balance_minor == 10000
        assert world.submission_count(unknown_id) == 1


def test_changed_seed_cash_refuses_atomically_and_keeps_real_history() -> None:
    from copy import deepcopy

    from aco.features.accounting.models import DemoReset
    from aco.features.identity.models import now

    with action_world() as world:
        world.client.app.include_router(router)
        pending_id = another_action(world, 0)
        baseline = world.store.scalar(
            select(DemoSourceRecord).where(
                DemoSourceRecord.tenant_id == world.ctx.tenant_id,
                DemoSourceRecord.provider_connection_id == world.connection_id,
                DemoSourceRecord.kind == "payment",
                DemoSourceRecord.external_id == "seed-INV-103/1",
                DemoSourceRecord.revision == 1,
            )
        )
        assert baseline is not None
        payload = deepcopy(baseline.payload)
        payload["amount"] = {"minor_units": 4000, "currency": "AUD", "exponent": 2}
        world.store.add(
            DemoSourceRecord(
                tenant_id=world.ctx.tenant_id,
                provider_connection_id=world.connection_id,
                organisation_id=baseline.organisation_id,
                kind="payment",
                external_id=baseline.external_id,
                revision=2,
                payload=payload,
                effective_at=now(),
                observed_at=now(),
            )
        )
        world.store.commit()
        tenant = world.store.get(Tenant, world.ctx.tenant_id)
        assert tenant is not None
        generation = tenant.version
        source_count = len(list(world.store.scalars(select(DemoSourceRecord.id))))
        world.store.rollback()
        response = world.client.post(
            f"/api/workspaces/{world.ctx.tenant_id}/demo/reset",
            json={
                "request_id": str(uuid4()),
                "expected_generation": generation,
                "evidence": "Owner reviewed unsupported original seed cash change",
            },
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == "DEMO_RESET_SOURCE_REVIEW_REQUIRED"
        world.store.rollback()
        tenant = world.store.get(Tenant, world.ctx.tenant_id, populate_existing=True)
        assert tenant is not None and tenant.version == generation
        assert world.action(pending_id).status == "queued"
        assert len(list(world.store.scalars(select(DemoSourceRecord.id)))) == source_count
        assert (
            world.store.scalar(select(DemoReset.id).where(DemoReset.tenant_id == world.ctx.tenant_id)) is None
        )


def test_reset_read_exposes_persisted_source_result_without_new_effect() -> None:
    from aco.features.accounting.models import DemoReset

    with action_world() as world:
        world.client.app.include_router(router)
        url = f"/api/workspaces/{world.ctx.tenant_id}/demo/reset"
        state = world.client.get(url)
        assert state.status_code == 200, state.text
        assert state.json()["can_reset"] and state.json()["last_reset"] is None
        generation = state.json()["generation"]
        response = world.client.post(
            url,
            json={
                "request_id": str(uuid4()),
                "expected_generation": generation,
                "evidence": "Owner reviewed actual source restoration",
            },
        )
        assert response.status_code == 200, response.text
        world.store.rollback()
        count = len(list(world.store.scalars(select(DemoSourceRecord.id))))
        world.store.rollback()
        read = world.client.get(url)
        assert read.status_code == 200, read.text
        assert read.json()["last_reset"] == response.json()
        assert read.json()["generation"] == generation + 1
        assert read.json()["outbound_paused"] and not read.json()["automation_enabled"]
        world.store.rollback()
        assert len(list(world.store.scalars(select(DemoSourceRecord.id)))) == count
        assert world.store.scalar(select(DemoReset.id).where(DemoReset.tenant_id == world.ctx.tenant_id))
