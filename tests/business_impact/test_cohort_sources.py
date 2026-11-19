"""Actual synthetic adapter history, current membership and immutable PostgreSQL cohorts."""

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from accounting_support import seed
from fastapi import FastAPI
from identity_support import actor_client
from sqlalchemy import select

from aco.features.accounting.models import InvoiceSnapshot, VerifiedPaymentAllocation
from aco.features.impact.cohort_api import router


def test_fixed_source_cohorts_have_reproducible_60_to_75_percent_and_exact_replay() -> None:
    with actor_client("owner") as (client, db, _user, tenant):
        app = client.app
        assert isinstance(app, FastAPI)
        app.include_router(router)
        seed(client, tenant)
        path = f"/api/workspaces/{tenant}/impact/cohorts"
        before = client.get(path)
        assert before.status_code == 200 and before.json()["can_seed"] is True
        command = {"request_id": str(uuid4()), "evidence": "Explicit fixed synthetic cohort illustration."}
        created = client.post(path + "/synthetic", json=command)
        assert created.status_code == 200, created.text
        result: dict[str, Any] = created.json()
        pair = result["pairs"][0]
        assert pair["comparison_allowed"] is True and pair["synthetic"] is True
        assert pair["recovery_change_pp"] == "15"
        assert pair["recovery_relative_change_percent"] == "25"
        baseline, after = pair["cohorts"]
        assert baseline["side"] == "BASELINE_SYNTHETIC" and after["side"] == "AFTER_SYNTHETIC"
        first, second = baseline["results"][-1], after["results"][-1]
        assert first["opening_minor"] == second["opening_minor"] == "40000"
        assert first["recovery_percent"] == "60" and second["recovery_percent"] == "75"
        assert first["paid_in_full_percent"] == "50" and second["paid_in_full_percent"] == "75"
        assert Decimal(first["days_to_collect"]) == 50 and Decimal(second["days_to_collect"]) == 40
        assert len(first["observation_ids"]) == len(second["observation_ids"]) == 4
        invoice_ids = [member["invoice_id"] for cohort in pair["cohorts"] for member in cohort["members"]]
        imported = list(db.scalars(select(InvoiceSnapshot).where(InvoiceSnapshot.tenant_id == tenant)))
        assert len([row for row in imported if str(row.id) in invoice_ids]) == 8
        allocations = list(
            db.scalars(select(VerifiedPaymentAllocation).where(VerifiedPaymentAllocation.tenant_id == tenant))
        )
        assert sum(row.minor_units for row in allocations if str(row.invoice_id) in invoice_ids) == 54000
        replay = client.post(path + "/synthetic", json=command)
        assert replay.status_code == 200 and replay.json()["pairs"] == result["pairs"]
        conflict = client.post(
            path + "/synthetic", json=command | {"evidence": "Changed exact command evidence."}
        )
        assert conflict.status_code == 409
        assert client.get(path).json()["pairs"] == result["pairs"]
        client.headers.pop("x-csrf-token", None)
        assert client.post(path + "/synthetic", json=command).status_code == 403


def test_viewer_cannot_create_history_or_cohort_and_foreign_tenant_read_is_denied() -> None:
    with actor_client("viewer") as (client, _db, _user, tenant):
        app = client.app
        assert isinstance(app, FastAPI)
        app.include_router(router)
        path = f"/api/workspaces/{tenant}/impact/cohorts"
        view = client.get(path)
        assert view.status_code == 200 and view.json()["can_seed"] is False
        response = client.post(
            path + "/synthetic",
            json={"request_id": str(uuid4()), "evidence": "Explicit synthetic data fixture."},
        )
        assert response.status_code == 403
        assert client.get(f"/api/workspaces/{uuid4()}/impact/cohorts").status_code == 403


@pytest.mark.parametrize("late_observed", [False, True])
def test_actual_refund_appends_restatement_and_due_correction_never_moves_membership(
    late_observed: bool,
) -> None:
    from datetime import timedelta
    from uuid import UUID

    import pytest
    from accounting_support import publish_invoice
    from sqlalchemy import delete, update
    from sqlalchemy.exc import DBAPIError

    from aco.features.accounting.models import DemoSourceRecord
    from aco.features.identity.models import now
    from aco.features.impact.models import CohortResult, InvoiceCohortMembership

    with actor_client("owner") as (client, db, _user, tenant):
        app = client.app
        assert isinstance(app, FastAPI)
        app.include_router(router)
        seed(client, tenant)
        path = f"/api/workspaces/{tenant}/impact/cohorts"
        created = client.post(
            path + "/synthetic",
            json={"request_id": str(uuid4()), "evidence": "Fixed source historical restatement fixture."},
        )
        assert created.status_code == 200, created.text
        pair = created.json()["pairs"][0]
        baseline = pair["cohorts"][0]
        invoice = db.scalar(
            select(InvoiceSnapshot).where(
                InvoiceSnapshot.tenant_id == tenant,
                InvoiceSnapshot.external_invoice_id == "COHORT-BASELINE_SYNTHETIC-1",
            )
        )
        assert invoice is not None
        cash = db.scalar(
            select(VerifiedPaymentAllocation).where(
                VerifiedPaymentAllocation.invoice_id == invoice.id, VerifiedPaymentAllocation.kind == "cash"
            )
        )
        assert cash is not None
        stamp = now()
        db.add(
            DemoSourceRecord(
                tenant_id=tenant,
                provider_connection_id=invoice.provider_connection_id,
                organisation_id=invoice.organisation_id,
                kind="payment",
                external_id="cohort-refund/1",
                revision=1,
                payload={
                    "external_payment_id": "cohort-refund",
                    "external_allocation_id": "1",
                    "external_invoice_id": invoice.external_invoice_id,
                    "kind": "refund",
                    "amount": {"minor_units": -2000, "currency": "AUD", "exponent": 2},
                    "parent_payment_id": cash.external_payment_id,
                    "parent_allocation_id": cash.external_allocation_id,
                },
                effective_at=cash.effective_at + timedelta(days=1) if late_observed else stamp,
                observed_at=stamp,
            )
        )
        db.commit()
        publish_invoice(
            db,
            tenant,
            invoice.provider_connection_id,
            key=invoice.external_invoice_id,
            outstanding={"minor_units": 2000, "currency": "AUD", "exponent": 2},
            status="partially_paid",
            due_date=str(invoice.due_date + timedelta(days=60)),
        )
        command = {
            "request_id": str(uuid4()),
            "expected_result_ids": [row["results"][-1]["id"] for row in pair["cohorts"]],
            "evidence": "Review actual late refund and preserve original membership.",
        }
        refreshed = client.post(path + f"/{pair['id']}/refresh", json=command)
        assert refreshed.status_code == 200, refreshed.text
        revised = refreshed.json()["pairs"][0]["cohorts"][0]
        assert revised["members"] == baseline["members"]
        assert revised["results"][0] == baseline["results"][0]
        latest = revised["results"][-1]
        assert latest["label"] == "RESTATED" and latest["version"] == 2
        assert latest["prior_result_id"] == baseline["results"][0]["id"]
        assert latest["opening_minor"] == "40000" and latest["recovery_percent"] == "55"
        assert latest["paid_in_full_percent"] == "25" and latest["refunds_minor"] == "2000"
        assert len(latest["adjustment_ids"]) == 1
        stale = client.post(path + f"/{pair['id']}/refresh", json=command | {"request_id": str(uuid4())})
        assert stale.status_code == 409
        replay = client.post(path + f"/{pair['id']}/refresh", json=command)
        assert replay.status_code == 200 and replay.json()["pairs"] == refreshed.json()["pairs"]
        for statement in (
            update(CohortResult).where(CohortResult.id == UUID(latest["id"])).values(result={}),
            delete(CohortResult).where(CohortResult.id == UUID(latest["id"])),
            delete(InvoiceCohortMembership).where(InvoiceCohortMembership.tenant_id == tenant),
        ):
            with pytest.raises(DBAPIError), db.begin_nested():
                db.execute(statement)


def test_current_owner_required_even_for_direct_synthetic_source_publication() -> None:
    import asyncio

    import pytest
    from accounting_support import context
    from fastapi import HTTPException

    from aco.features.identity.models import Membership
    from aco.integrations.demo_accounting.history import DemoCohortAdapter

    with actor_client("owner") as (client, db, user, tenant):
        app = client.app
        assert isinstance(app, FastAPI)
        seeded = seed(client, tenant)
        member = db.scalar(
            select(Membership).where(Membership.tenant_id == tenant, Membership.user_id == user.id)
        )
        assert member is not None
        member.role = "viewer"
        db.commit()
        from uuid import UUID

        async def attempt() -> None:
            async with app.state.business_sessions.begin() as session:
                adapter = DemoCohortAdapter(session, UUID(seeded["provider_connection_id"]))
                await adapter.publish_fixed_history(context(tenant, user.id), "Australia/Melbourne", "local")

        with pytest.raises(HTTPException) as denied:
            asyncio.run(attempt())
        assert denied.value.status_code == 403


def test_inconsistent_source_correction_keeps_frozen_denominator_and_marks_comparison_unavailable() -> None:
    from accounting_support import publish_invoice

    with actor_client("owner") as (client, db, _user, tenant):
        app = client.app
        assert isinstance(app, FastAPI)
        app.include_router(router)
        seed(client, tenant)
        path = f"/api/workspaces/{tenant}/impact/cohorts"
        seeded = client.post(
            path + "/synthetic",
            json={
                "request_id": str(uuid4()),
                "evidence": "Fixed synthetic source opening correction fixture.",
            },
        )
        assert seeded.status_code == 200, seeded.text
        pair = seeded.json()["pairs"][0]
        first = pair["cohorts"][0]
        invoice = db.scalar(
            select(InvoiceSnapshot).where(
                InvoiceSnapshot.tenant_id == tenant,
                InvoiceSnapshot.external_invoice_id == "COHORT-BASELINE_SYNTHETIC-1",
            )
        )
        assert invoice is not None
        publish_invoice(
            db,
            tenant,
            invoice.provider_connection_id,
            key=invoice.external_invoice_id,
            original={"minor_units": 12000, "currency": "AUD", "exponent": 2},
            outstanding={"minor_units": 2000, "currency": "AUD", "exponent": 2},
            status="partially_paid",
        )
        response = client.post(
            path + f"/{pair['id']}/refresh",
            json={
                "request_id": str(uuid4()),
                "expected_result_ids": [row["results"][-1]["id"] for row in pair["cohorts"]],
                "evidence": "Review corrected source original amount without replacing opening exposure.",
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()["pairs"][0]
        assert result["comparison_allowed"] is False and result["recovery_change_pp"] is None
        baseline = result["cohorts"][0]
        assert baseline["members"] == first["members"] and baseline["results"][0] == first["results"][0]
        latest = baseline["results"][-1]
        assert latest["status"] == latest["label"] == "INCOMPLETE"
        assert latest["opening_minor"] == "40000" and latest["eligible_count"] == 4
        assert latest["recovery_percent"] is None and latest["paid_in_full_count"] is None
        assert any("opening_history_review" in item for item in latest["issues"])
