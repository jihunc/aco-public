"""Promise results consume real accounting observations, never natural-language payment claims."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from accounting_support import context
from boundary_evidence import BoundaryObserver
from case_promise_support import communication, invoices
from fastapi import FastAPI
from identity_support import actor_client
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aco.core.clock import FrozenClock
from aco.core.context import TenantContext
from aco.core.types import Money
from aco.features.accounting.ingest import AccountingService
from aco.features.accounting.models import (
    AccountingObservation,
    CollectionRecord,
    DemoSourceRecord,
    InvoiceSnapshot,
)
from aco.features.cases.dto import CommunicationEvidence
from aco.features.promises.dto import ConfirmPromise, RecordPromise
from aco.features.promises.service import PromiseService
from aco.integrations.demo_accounting.adapter import DemoAccountingAdapter


def test_verified_partial_invoice_cash_fulfils_promise_once_and_stale_recheck_preserves_history() -> None:
    from aco.features.promises.models import PromiseAllocationClaim, PromiseVerification
    from aco.features.promises.performance import verify_invoice_promises

    with actor_client("owner") as (client, db, user, tenant):
        invoice = invoices(client, db, tenant)[0]
        msg = communication(
            db, tenant, [invoice], "SYNTHETIC promise: AUD40 tomorrow; not a real customer statement."
        )
        ctx = context(tenant, user.id)
        evidence = CommunicationEvidence(
            communication_id=msg.id, excerpt=str(msg.content["text_body"]), offset=0
        )
        svc = PromiseService(db)
        captured = svc.record_promise(
            ctx,
            RecordPromise(
                customer_id=invoice.customer_id,
                invoice_ids=[invoice.id],
                evidence=evidence,
                amount=Money(minor_units=4000, currency="AUD", exponent=2),
                due_date=(datetime.now(UTC) + timedelta(days=1)).date(),
                timezone="UTC",
            ),
        )
        confirmed = svc.confirm_promise(
            ctx,
            ConfirmPromise(
                promise_id=captured.id,
                expected_version=captured.version,
                confirmation_evidence=evidence,
                reason="Explicit synthetic documentary confirmation of this exact commitment.",
            ),
        )
        db.commit()
        response = client.post(
            f"/api/workspaces/{tenant}/demo/accounting/simulate-payment",
            json={
                "provider_connection_id": str(invoice.provider_connection_id),
                "external_invoice_id": invoice.external_invoice_id,
                "operation_id": str(uuid4()),
                "amount": {"minor_units": 4000, "currency": "AUD", "exponent": 2},
            },
        )
        assert response.status_code == 200, response.text
        db.rollback()
        observation = db.scalar(
            select(AccountingObservation)
            .where(AccountingObservation.invoice_id == invoice.id)
            .order_by(AccountingObservation.created_at.desc())
        )
        assert observation is not None
        verify_invoice_promises(db, ctx, observation.id)
        db.commit()
        view = svc.get(ctx, confirmed.id)
        assert view.status == "FULFILLED"
        current = db.get(InvoiceSnapshot, invoice.id, populate_existing=True)
        collection = db.scalar(select(CollectionRecord).where(CollectionRecord.invoice_id == invoice.id))
        assert current is not None and current.balance_minor > 0
        assert collection is not None and collection.lifecycle == "active"
        assert "active_promise" not in collection.hold_reasons
        assert db.scalar(select(func.count()).select_from(PromiseAllocationClaim)) == 1
        count = db.scalar(select(func.count()).select_from(PromiseVerification))
        verify_invoice_promises(db, ctx, observation.id)
        assert db.scalar(select(func.count()).select_from(PromiseVerification)) == count
        db.commit()
        verify_invoice_promises(
            db, ctx, observation.id, clock=FrozenClock(observation.observed_at + timedelta(minutes=16))
        )
        db.commit()
        view = svc.get(ctx, confirmed.id)
        assert view.status == "FULFILLED"
        assert view.verification_status == "PENDING_REVIEW"
        db.refresh(collection)
        assert "promise_verification" in collection.hold_reasons

        from aco.features.review.models import CollectionTask

        review = db.get(CollectionTask, view.review_task_id)
        assert review is not None and review.title == "Verify payment promise performance"


def source_cash(
    db: Session,
    invoice: InvoiceSnapshot,
    key: str,
    amount: int,
    effective: datetime,
    *,
    parent: str | None = None,
) -> None:
    db.add(
        DemoSourceRecord(
            tenant_id=invoice.tenant_id,
            provider_connection_id=invoice.provider_connection_id,
            organisation_id=invoice.organisation_id,
            kind="payment",
            external_id=key + "/1",
            revision=1,
            payload={
                "external_payment_id": key,
                "external_allocation_id": "1",
                "external_invoice_id": invoice.external_invoice_id,
                "kind": "reversal" if parent else "cash",
                "amount": {"minor_units": amount, "currency": invoice.currency, "exponent": invoice.exponent},
                "parent_payment_id": parent,
                "parent_allocation_id": "1" if parent else None,
            },
            effective_at=effective,
            observed_at=effective,
        )
    )
    db.commit()


def actual_read(app: FastAPI, ctx: TenantContext, invoice: InvoiceSnapshot, at: datetime) -> UUID:
    async def run() -> UUID:
        async with app.state.business_sessions.begin() as session:
            service = AccountingService(
                session, DemoAccountingAdapter(session, invoice.provider_connection_id), FrozenClock(at)
            )
            await service.recheck_invoice(ctx, invoice.external_invoice_id)
            assert isinstance(service.last_observation_id, UUID)
            return service.last_observation_id

    return asyncio.run(run())


REVERSAL_FIXTURES = [
    case
    for case in json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]
    if case["id"] == "promise_reversed_performance_reopens_missed"
    or (
        case["id"].startswith("promise_reversal_")
        and (
            "residual_before" in case["id"]
            or "residual_after" in case["id"]
            or case["id"]
            in {
                "promise_reversal_stale_retains_verification_hold",
                "promise_reversal_excludes_wrong_invoice_cash",
                "promise_reversal_excludes_already_reused_cash",
            }
        )
    )
]


@pytest.mark.parametrize("fixture", REVERSAL_FIXTURES, ids=[case["id"] for case in REVERSAL_FIXTURES])
def test_boundary_reversal_residual_preserves_history_and_independent_holds(
    monkeypatch: pytest.MonkeyPatch,
    fixture: dict[str, Any],
    boundary_observer: BoundaryObserver,
) -> None:
    from accounting_support import publish_invoice

    from aco.features.cases.service import place_hold
    from aco.features.communications.models import Communication
    from aco.features.promises.models import PromiseEvent, PromiseVerification
    from aco.features.promises.performance import verify_invoice_promises

    data, expected = fixture["input"], fixture["expected"]
    confirmed_at = datetime.fromisoformat(data["confirmed_at"].replace("Z", "+00:00"))
    cutoff = datetime.fromisoformat(data["cutoff"].replace("Z", "+00:00"))
    at = datetime.fromisoformat(data["as_of"].replace("Z", "+00:00"))
    with actor_client("owner") as (client, db, user, tenant):
        available = invoices(client, db, tenant)
        invoice = available[0]
        app = cast(FastAPI, client.app)
        msg = communication(
            db, tenant, [invoice], "SYNTHETIC fixture: AUD40 by 2026-11-20 Melbourne, explicit test source."
        )
        ctx = context(tenant, user.id)
        evidence = CommunicationEvidence(
            communication_id=msg.id, excerpt=str(msg.content["text_body"]), offset=0
        )
        monkeypatch.setattr("aco.features.promises.service.now", lambda: confirmed_at)
        svc = PromiseService(db)
        captured = svc.record_promise(
            ctx,
            RecordPromise(
                customer_id=invoice.customer_id,
                invoice_ids=[invoice.id],
                evidence=evidence,
                amount=Money(minor_units=data["amount_minor_units"], currency="AUD", exponent=2),
                due_date=cutoff.date(),
                timezone="Australia/Melbourne",
            ),
        )
        promise = svc.confirm_promise(
            ctx,
            ConfirmPromise(
                promise_id=captured.id,
                expected_version=captured.version,
                confirmation_evidence=evidence,
                reason="Authorized explicit synthetic documentary confirmation.",
            ),
        )
        assert promise.confirmed_at == confirmed_at and promise.cutoff_at == cutoff
        place_hold(
            db, ctx, invoice.id, "active_dispute", "Independent synthetic dispute fixture remains unresolved."
        )
        db.commit()
        first = data["allocations"][0]
        cash_at = datetime.fromisoformat(first["effective_at"].replace("Z", "+00:00"))
        source_cash(db, invoice, first["id"], first["minor_units"], cash_at)
        before = min(at, cutoff) - timedelta(seconds=2)
        monkeypatch.setattr("aco.integrations.demo_accounting.adapter.now", lambda: before)
        publish_invoice(
            db,
            tenant,
            invoice.provider_connection_id,
            invoice.external_invoice_id,
            outstanding={"minor_units": 5000, "currency": "AUD", "exponent": 2},
            status="partially_paid",
            source_status="PARTIALLY_PAID",
        )
        db.rollback()
        obs = actual_read(app, ctx, invoice, before)
        verify_invoice_promises(db, ctx, obs, clock=FrozenClock(before))
        db.commit()
        assert svc.get(ctx, promise.id).status == "FULFILLED"
        db.commit()
        extra_allocation_id = None
        if len(data["allocations"]) > 1:
            from aco.features.accounting.models import VerifiedPaymentAllocation

            extra = data["allocations"][1]
            extra_invoice = available[1] if extra["invoice"] != data["invoice"] else invoice
            extra_promise = None
            if extra["used_by_other_promise"]:
                extra_msg = communication(
                    db, tenant, [invoice], "SYNTHETIC separate commitment: AUD20 by 20 November."
                )
                extra_evidence = CommunicationEvidence(
                    communication_id=extra_msg.id, excerpt=str(extra_msg.content["text_body"]), offset=0
                )
                monkeypatch.setattr(
                    "aco.features.promises.service.now", lambda: confirmed_at + timedelta(seconds=1)
                )
                captured_extra = svc.record_promise(
                    ctx,
                    RecordPromise(
                        customer_id=invoice.customer_id,
                        invoice_ids=[invoice.id],
                        evidence=extra_evidence,
                        amount=Money(minor_units=extra["minor_units"], currency="AUD", exponent=2),
                        due_date=cutoff.date(),
                        timezone="Australia/Melbourne",
                    ),
                )
                extra_promise = svc.confirm_promise(
                    ctx,
                    ConfirmPromise(
                        promise_id=captured_extra.id,
                        expected_version=captured_extra.version,
                        confirmation_evidence=extra_evidence,
                        reason="Separate explicit synthetic commitment review.",
                    ),
                )
                db.commit()
            source_cash(
                db,
                extra_invoice,
                extra["id"],
                extra["minor_units"],
                datetime.fromisoformat(extra["effective_at"].replace("Z", "+00:00")),
            )
            publish_invoice(
                db,
                tenant,
                extra_invoice.provider_connection_id,
                extra_invoice.external_invoice_id,
                outstanding={
                    "minor_units": 3000 if extra["used_by_other_promise"] else 8000,
                    "currency": "AUD",
                    "exponent": 2,
                },
                status="partially_paid",
                source_status="PARTIALLY_PAID",
            )
            db.rollback()
            extra_observation = actual_read(app, ctx, extra_invoice, before)
            verify_invoice_promises(db, ctx, extra_observation, clock=FrozenClock(before))
            db.commit()
            if extra_promise:
                assert svc.get(ctx, extra_promise.id).status == "FULFILLED"
            extra_allocation = db.scalar(
                select(VerifiedPaymentAllocation).where(
                    VerifiedPaymentAllocation.invoice_id == extra_invoice.id,
                    VerifiedPaymentAllocation.external_payment_id == extra["id"],
                )
            )
            assert extra_allocation is not None
            extra_allocation_id = str(extra_allocation.id)
            db.commit()
        reversal = data["reversals"][0]
        reversal_at = datetime.fromisoformat(reversal["effective_at"].replace("Z", "+00:00"))
        source_cash(db, invoice, reversal["id"], -reversal["minor_units"], reversal_at, parent=first["id"])
        publish_invoice(
            db,
            tenant,
            invoice.provider_connection_id,
            invoice.external_invoice_id,
            outstanding={
                "minor_units": data["source_outstanding_minor_units"],
                "currency": "AUD",
                "exponent": 2,
            },
            status=data.get("source_financial_status", "partially_paid"),
            source_status="PARTIALLY_PAID",
        )
        read_at = at if data["source_fresh"] else at - timedelta(minutes=16)
        monkeypatch.setattr("aco.integrations.demo_accounting.adapter.now", lambda: read_at)
        db.rollback()
        obs = actual_read(app, ctx, invoice, read_at)
        verify_invoice_promises(db, ctx, obs, clock=FrozenClock(at))
        db.commit()
        view = svc.get(ctx, promise.id)
        check = db.scalar(
            select(PromiseVerification)
            .where(PromiseVerification.promise_id == promise.id)
            .order_by(PromiseVerification.created_at.desc())
        )
        collection = db.scalar(
            select(CollectionRecord)
            .where(CollectionRecord.invoice_id == invoice.id)
            .execution_options(populate_existing=True)
        )
        snapshot = db.get(InvoiceSnapshot, invoice.id, populate_existing=True)
        assert check is not None and collection is not None and snapshot is not None
        from aco.features.accounting.models import VerifiedPaymentAllocation
        from aco.features.review.models import CollectionTask
        from aco.integrations.demo_email.models import NativeEmailOperation

        counted_ids = check.details["allocation_ids"]
        assert isinstance(counted_ids, list)
        if extra_allocation_id is not None:
            assert extra_allocation_id not in counted_ids
            if data["allocations"][1]["used_by_other_promise"]:
                excluded_ids = check.details["excluded_allocation_ids"]
                assert isinstance(excluded_ids, list)
                assert extra_allocation_id in excluded_ids
        events = list(db.scalars(select(PromiseEvent).where(PromiseEvent.promise_id == promise.id)))
        task = db.get(CollectionTask, view.review_task_id) if view.review_task_id else None
        excluded = (
            list(
                db.scalars(
                    select(VerifiedPaymentAllocation).where(
                        VerifiedPaymentAllocation.id == extra_allocation_id
                    )
                )
            )
            if extra_allocation_id
            else []
        )
        values: dict[str, object] = {
            "status": view.status,
            "timely_performance_minor_units": check.timely_minor,
            "verification_status": check.verification_status,
            "promise_hold_active": any(
                reason in collection.hold_reasons for reason in ("active_promise", "promise_verification")
            ),
            "hold_reasons": collection.hold_reasons,
            "excluded_allocation_ids": [
                row.external_payment_id for row in excluded if str(row.id) not in counted_ids
            ],
            "new_missed_episode_count": sum(
                e.event == "promise.installment_status" and e.details["status"] == "MISSED" for e in events
            ),
            "prior_fulfilled_history_preserved": any(
                e.event == "promise.installment_status" and e.details["status"] == "FULFILLED" for e in events
            ),
            "reversal_evidence_count": len(cast(list[str], check.details["reversal_ids"])),
            "collection_lifecycle": collection.lifecycle,
            "source_outstanding_minor_units": snapshot.balance_minor,
            "status_changed": view.status != data["prior_status"],
            "current_verification_result": check.details["performance"],
            "review_task_required": task is not None and task.title == "Verify payment promise performance",
            "backlog_sends": db.scalar(
                select(func.count()).select_from(Communication).where(Communication.direction == "outgoing")
            ),
            "actual_provider_submissions": db.scalar(select(func.count()).select_from(NativeEmailOperation)),
        }
        assert "active_dispute" in collection.hold_reasons
        assert snapshot.balance_minor == data["source_outstanding_minor_units"]
        actual = {key: values[key] for key in expected}
        assert actual == expected
        boundary_observer.assert_observed(fixture["id"], actual)


@pytest.mark.parametrize(
    "scenario", ["outside_window", "exact_cutoff", "mixed_late", "observed_late", "overlapping"]
)
def test_exact_performance_interval_and_nonreuse(
    monkeypatch: pytest.MonkeyPatch, scenario: str, boundary_observer: BoundaryObserver
) -> None:
    from accounting_support import publish_invoice

    from aco.features.promises.models import PromiseAllocationClaim, PromiseVerification
    from aco.features.promises.performance import verify_invoice_promises

    confirmed_at = datetime(2026, 11, 17, tzinfo=UTC)
    cutoff = datetime(2026, 11, 20, 13, tzinfo=UTC)
    at = cutoff + timedelta(hours=1)
    fixture = next(
        row
        for row in json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]
        if row["id"] == "promise_source_allocation_not_reused"
    )
    amount = fixture["input"]["promises"][0]["amount_minor_units"] if scenario == "overlapping" else 4000
    cash_key = fixture["input"]["verified_allocations"][0]["id"] if scenario == "overlapping" else "CASH"
    with actor_client("owner") as (client, db, user, tenant):
        invoice = invoices(client, db, tenant)[0]
        app = cast(FastAPI, client.app)
        ctx = context(tenant, user.id)
        svc = PromiseService(db)
        promises = []
        for index in range(2 if scenario == "overlapping" else 1):
            msg = communication(
                db,
                tenant,
                [invoice],
                f"SYNTHETIC explicit commitment {index}: AUD{amount // 100} by 2026-11-20.",
            )
            evidence = CommunicationEvidence(
                communication_id=msg.id, excerpt=str(msg.content["text_body"]), offset=0
            )
            monkeypatch.setattr(
                "aco.features.promises.service.now",
                lambda index=index: confirmed_at + timedelta(seconds=index),
            )
            captured = svc.record_promise(
                ctx,
                RecordPromise(
                    customer_id=invoice.customer_id,
                    invoice_ids=[invoice.id],
                    evidence=evidence,
                    amount=Money(minor_units=amount, currency="AUD", exponent=2),
                    due_date=cutoff.date(),
                    timezone="Australia/Melbourne",
                ),
            )
            promises.append(
                svc.confirm_promise(
                    ctx,
                    ConfirmPromise(
                        promise_id=captured.id,
                        expected_version=captured.version,
                        confirmation_evidence=evidence,
                        reason="Explicit reviewed synthetic scope and authority.",
                    ),
                )
            )
            db.commit()
        if scenario == "mixed_late":
            source_cash(db, invoice, "TIMELY", 2000, cutoff - timedelta(seconds=1))
            source_cash(db, invoice, "LATE", 2000, cutoff)
        else:
            effective = (
                confirmed_at - timedelta(seconds=1)
                if scenario == "outside_window"
                else cutoff
                if scenario == "exact_cutoff"
                else cutoff - timedelta(seconds=1)
            )
            source_cash(db, invoice, cash_key, amount, effective)
        publish_invoice(
            db,
            tenant,
            invoice.provider_connection_id,
            invoice.external_invoice_id,
            outstanding={"minor_units": 10000 - amount, "currency": "AUD", "exponent": 2},
            status="partially_paid" if amount < 10000 else "paid",
            source_status="PARTIALLY_PAID" if amount < 10000 else "PAID",
        )
        monkeypatch.setattr("aco.integrations.demo_accounting.adapter.now", lambda: at)
        db.rollback()
        observation_id = actual_read(app, ctx, invoice, at)
        verify_invoice_promises(db, ctx, observation_id, clock=FrozenClock(at))
        db.commit()
        view = svc.get(ctx, promises[0].id)
        result = db.scalar(
            select(PromiseVerification)
            .where(PromiseVerification.promise_id == view.id)
            .order_by(PromiseVerification.created_at.desc())
        )
        assert result is not None
        if scenario in {"observed_late", "overlapping"}:
            assert view.status == "FULFILLED" and result.timely_minor == amount
        else:
            assert view.status == "MISSED"
            assert result.details["performance"] == (
                "OUTSIDE_WINDOW" if scenario == "outside_window" else "LATE_FULFILLED"
            )
            assert result.timely_minor == (2000 if scenario == "mixed_late" else 0)
        if scenario == "overlapping":
            second = svc.get(ctx, promises[1].id)
            result2 = db.scalar(
                select(PromiseVerification).where(PromiseVerification.promise_id == second.id)
            )
            assert second.status == "MISSED" and result2 is not None and result2.timely_minor == 0
            assert len(cast(list[str], result2.details["excluded_allocation_ids"])) == 1
            assert db.scalar(select(func.count()).select_from(PromiseAllocationClaim)) == 1
            from sqlalchemy.exc import DBAPIError

            claim = db.scalar(select(PromiseAllocationClaim))
            assert claim is not None
            with pytest.raises(DBAPIError, match="already belongs to another promise"):
                with db.begin_nested():
                    db.add(
                        PromiseAllocationClaim(
                            tenant_id=tenant,
                            provider_connection_id=invoice.provider_connection_id,
                            allocation_id=claim.allocation_id,
                            claimed_minor=1,
                            promise_id=second.id,
                            installment_id=second.installments[0].id,
                        )
                    )
                    db.flush()
            with pytest.raises(DBAPIError, match="source cash overclaimed"):
                with db.begin_nested():
                    db.add(
                        PromiseAllocationClaim(
                            tenant_id=tenant,
                            provider_connection_id=invoice.provider_connection_id,
                            allocation_id=claim.allocation_id,
                            claimed_minor=1,
                            promise_id=view.id,
                            installment_id=view.installments[0].id,
                        )
                    )
                    db.flush()

            from aco.features.accounting.models import VerifiedPaymentAllocation

            claim_ids = set(db.scalars(select(PromiseAllocationClaim.allocation_id)))
            source_ids = set(
                db.scalars(
                    select(VerifiedPaymentAllocation.id).where(
                        VerifiedPaymentAllocation.invoice_id == invoice.id,
                        VerifiedPaymentAllocation.external_payment_id == cash_key,
                    )
                )
            )
            assert len(source_ids) == len(fixture["input"]["verified_allocations"])
            boundary_observer.assert_observed(
                fixture["id"],
                {
                    "fulfilled_promises_max": sum(
                        svc.get(ctx, item.id).status == "FULFILLED" for item in promises
                    ),
                    "invented_allocations": len(claim_ids - source_ids),
                },
            )


@pytest.mark.parametrize("after_cutoff,late_first", [(False, False), (True, False), (False, True)])
def test_one_cash_allocation_performs_two_explicit_installments_and_reversal_uses_residual_once(
    monkeypatch: pytest.MonkeyPatch,
    after_cutoff: bool,
    late_first: bool,
) -> None:
    from accounting_support import publish_invoice

    from aco.features.promises.dto import PromiseInstallmentCommand
    from aco.features.promises.models import PromiseAllocationClaim, PromiseVerification
    from aco.features.promises.performance import verify_invoice_promises

    confirmed_at = datetime(2026, 11, 17, tzinfo=UTC)
    cash_at = datetime(2026, 11, 21 if late_first else 20, 12, tzinfo=UTC)
    with actor_client("owner") as (client, db, user, tenant):
        invoice = invoices(client, db, tenant)[0]
        ctx = context(tenant, user.id)
        app = cast(FastAPI, client.app)
        msg = communication(
            db, tenant, [invoice], "SYNTHETIC two explicit installments: AUD40 on20Nov and AUD60 on21Nov."
        )
        evidence = CommunicationEvidence(
            communication_id=msg.id, excerpt=str(msg.content["text_body"]), offset=0
        )
        svc = PromiseService(db)
        monkeypatch.setattr("aco.features.promises.service.now", lambda: confirmed_at)
        captured = svc.record_promise(
            ctx,
            RecordPromise(
                customer_id=invoice.customer_id,
                invoice_ids=[invoice.id],
                evidence=evidence,
                amount=Money(minor_units=10000, currency="AUD", exponent=2),
                due_date=datetime(2026, 11, 21).date(),
                timezone="Australia/Melbourne",
                installments=[
                    PromiseInstallmentCommand(
                        due_date=datetime(2026, 11, 20).date(),
                        amount=Money(minor_units=4000, currency="AUD", exponent=2),
                    ),
                    PromiseInstallmentCommand(
                        due_date=datetime(2026, 11, 21).date(),
                        amount=Money(minor_units=6000, currency="AUD", exponent=2),
                    ),
                ],
            ),
        )
        promise = svc.confirm_promise(
            ctx,
            ConfirmPromise(
                promise_id=captured.id,
                expected_version=captured.version,
                confirmation_evidence=evidence,
                reason="Explicit reviewed two-installment synthetic commitment.",
            ),
        )
        db.commit()
        source_cash(db, invoice, "ONE-CASH", 10000, cash_at)
        publish_invoice(
            db,
            tenant,
            invoice.provider_connection_id,
            invoice.external_invoice_id,
            outstanding={"minor_units": 0, "currency": "AUD", "exponent": 2},
            status="paid",
            source_status="PAID",
        )
        before = cash_at + timedelta(minutes=1)
        monkeypatch.setattr("aco.integrations.demo_accounting.adapter.now", lambda: before)
        observation = actual_read(app, ctx, invoice, before)
        verify_invoice_promises(db, ctx, observation, clock=FrozenClock(before))
        db.commit()
        view = svc.get(ctx, promise.id)
        assert view.status == ("MISSED" if late_first else "FULFILLED")
        assert [item.status for item in view.installments] == [
            "MISSED" if late_first else "FULFILLED",
            "FULFILLED",
        ]
        claims = list(
            db.scalars(select(PromiseAllocationClaim).where(PromiseAllocationClaim.promise_id == promise.id))
        )
        assert len(claims) == 2 and len({claim.allocation_id for claim in claims}) == 1
        assert sorted(claim.claimed_minor for claim in claims) == [4000, 6000]
        db.commit()
        at = datetime(2026, 11, 22, 14, tzinfo=UTC) if after_cutoff else cash_at + timedelta(minutes=30)
        source_cash(db, invoice, "REFUND", -1000, at - timedelta(seconds=1), parent="ONE-CASH")
        publish_invoice(
            db,
            tenant,
            invoice.provider_connection_id,
            invoice.external_invoice_id,
            outstanding={"minor_units": 1000, "currency": "AUD", "exponent": 2},
            status="partially_paid",
            source_status="PARTIALLY_PAID",
        )
        monkeypatch.setattr("aco.integrations.demo_accounting.adapter.now", lambda: at)
        observation = actual_read(app, ctx, invoice, at)
        verify_invoice_promises(db, ctx, observation, clock=FrozenClock(at))
        db.commit()
        view = svc.get(ctx, promise.id)
        assert view.status == ("MISSED" if after_cutoff or late_first else "ACTIVE")
        assert [item.status for item in view.installments] == [
            "MISSED" if late_first else "FULFILLED",
            "MISSED" if after_cutoff else "ACTIVE",
        ]
        latest = [
            db.scalar(
                select(PromiseVerification)
                .where(PromiseVerification.installment_id == item.id)
                .order_by(PromiseVerification.created_at.desc())
            )
            for item in view.installments
        ]
        assert all(row is not None for row in latest)
        assert [row.timely_minor for row in latest if row is not None] == [0 if late_first else 4000, 5000]
        if late_first:
            assert latest[0] is not None and latest[0].late_minor == 4000
        assert (
            sum(
                claim.claimed_minor
                for claim in db.scalars(
                    select(PromiseAllocationClaim).where(PromiseAllocationClaim.promise_id == promise.id)
                )
            )
            == 10000
        )


def test_failed_source_recording_requires_actual_executing_promise_worker() -> None:
    from fastapi import HTTPException

    from aco.features.promises.models import PromiseVerification
    from aco.features.promises.performance import record_promise_source_failure

    with actor_client("owner") as (client, db, user, tenant):
        invoice = invoices(client, db, tenant)[0]
        with pytest.raises(HTTPException) as denied:
            record_promise_source_failure(
                db, context(tenant, user.id), invoice.id, "TIMEOUT", rejection_evidence=None
            )
        assert denied.value.status_code == 403
        assert db.scalar(select(func.count()).select_from(PromiseVerification)) == 0


INTERVAL_FIXTURES = [
    case
    for case in json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]
    if case["id"]
    in {
        "promise_paid_by_preconfirmation_cash_not_fulfilled",
        "promise_partial_commitment_full_performance_invoice_open",
        "promise_exact_cutoff_cash_is_late",
        "promise_after_cutoff_cash_preserves_missed",
        "promise_full_commitment_within_window",
    }
]


@pytest.mark.parametrize("fixture", INTERVAL_FIXTURES, ids=[case["id"] for case in INTERVAL_FIXTURES])
def test_normative_interval_fixture_with_real_source_and_pending_policy(
    monkeypatch: pytest.MonkeyPatch, fixture: dict[str, Any], boundary_observer: BoundaryObserver
) -> None:
    from accounting_support import publish_invoice
    from integration.test_policy_authority import setup_active
    from policy_support import policy_world

    from aco.features.actions.models import ScheduledAction
    from aco.features.communications.models import Communication
    from aco.features.promises.models import PromiseEvent, PromiseVerification
    from aco.features.promises.performance import verify_invoice_promises

    data, expected = fixture["input"], fixture["expected"]
    confirmed_at = datetime.fromisoformat(data["confirmed_at"].replace("Z", "+00:00"))
    cutoff = datetime.fromisoformat(data["cutoff"].replace("Z", "+00:00"))
    at = datetime.fromisoformat(data["as_of"].replace("Z", "+00:00"))
    with policy_world() as (client, db, ctx, available, saved):
        invoice = next(item for item in available if item.number == "INV-101")
        setup_active(client, db, ctx, saved)
        queued = list(
            db.scalars(
                select(ScheduledAction).where(
                    ScheduledAction.invoice_id == invoice.id,
                    ScheduledAction.kind == "policy_due",
                    ScheduledAction.status == "queued",
                )
            )
        )
        assert queued
        queued_ids = [item.id for item in queued]
        db.commit()
        msg = communication(
            db, ctx.tenant_id, [invoice], "SYNTHETIC explicit AUD40 by20November2026 commitment."
        )
        evidence = CommunicationEvidence(
            communication_id=msg.id, excerpt=str(msg.content["text_body"]), offset=0
        )
        monkeypatch.setattr("aco.features.promises.service.now", lambda: confirmed_at)
        service = PromiseService(db)
        captured = service.record_promise(
            ctx,
            RecordPromise(
                customer_id=invoice.customer_id,
                invoice_ids=[invoice.id],
                evidence=evidence,
                amount=Money(minor_units=data["amount_minor_units"], currency=data["currency"], exponent=2),
                due_date=cutoff.date(),
                timezone="Australia/Melbourne",
            ),
        )
        promise = service.confirm_promise(
            ctx,
            ConfirmPromise(
                promise_id=captured.id,
                expected_version=captured.version,
                confirmation_evidence=evidence,
                reason="Explicit synthetic documentary review of exact fixture terms.",
            ),
        )
        assert promise.cutoff_at == cutoff
        db.commit()
        app = cast(FastAPI, client.app)
        if data.get("prior_status") == "MISSED":
            prior_at = cutoff + timedelta(seconds=1)
            monkeypatch.setattr("aco.integrations.demo_accounting.adapter.now", lambda: prior_at)
            prior_observation = actual_read(app, ctx, invoice, prior_at)
            verify_invoice_promises(db, ctx, prior_observation, clock=FrozenClock(prior_at))
            db.commit()
            assert service.get(ctx, promise.id).status == "MISSED"
            db.commit()
        for allocation in data["allocations"]:
            source_cash(
                db,
                invoice,
                allocation["id"],
                allocation["minor_units"],
                datetime.fromisoformat(allocation["effective_at"].replace("Z", "+00:00")),
            )
        publish_invoice(
            db,
            ctx.tenant_id,
            invoice.provider_connection_id,
            invoice.external_invoice_id,
            outstanding={
                "minor_units": data["source_outstanding_minor_units"],
                "currency": data["currency"],
                "exponent": 2,
            },
            status=data["source_financial_status"],
            source_status=data["source_financial_status"].upper(),
        )
        monkeypatch.setattr("aco.integrations.demo_accounting.adapter.now", lambda: at)
        observation_id = actual_read(app, ctx, invoice, at)
        verify_invoice_promises(db, ctx, observation_id, clock=FrozenClock(at))
        db.commit()
        view = service.get(ctx, promise.id)
        check = db.scalar(
            select(PromiseVerification)
            .where(PromiseVerification.promise_id == promise.id)
            .order_by(PromiseVerification.created_at.desc(), PromiseVerification.id.desc())
        )
        collection = db.scalar(select(CollectionRecord).where(CollectionRecord.invoice_id == invoice.id))
        snapshot = db.get(InvoiceSnapshot, invoice.id, populate_existing=True)
        assert check is not None and collection is not None and snapshot is not None
        assert view.status == expected["status"]
        assert check.timely_minor == expected["timely_performance_minor_units"]
        assert check.details["performance"] == expected["performance_status"]
        assert collection.lifecycle == expected["collection_lifecycle"]
        if "late_performance_minor_units" in expected:
            assert check.late_minor == expected["late_performance_minor_units"]
        if "promise_hold_active" in expected:
            assert ("active_promise" in collection.hold_reasons) == expected["promise_hold_active"]
        if "missed_history_preserved" in expected:
            assert (
                bool(
                    db.scalar(
                        select(PromiseEvent).where(
                            PromiseEvent.promise_id == promise.id,
                            PromiseEvent.event == "promise.installment_status",
                            PromiseEvent.details["status"].as_string() == "MISSED",
                        )
                    )
                )
                == expected["missed_history_preserved"]
            )
        if "pending_outreach_cancelled" in expected:
            persisted_actions = [
                db.get(ScheduledAction, ident, populate_existing=True) for ident in queued_ids
            ]
            assert all(item is not None for item in persisted_actions)
            assert (
                all(item.status == "cancelled" for item in persisted_actions if item is not None)
                == expected["pending_outreach_cancelled"]
            )
        assert snapshot.balance_minor == data["source_outstanding_minor_units"]
        assert snapshot.financial_status == data["source_financial_status"]
        if "financial_status_mutated_by_aco" in expected:
            observation = db.get(AccountingObservation, observation_id)
            assert observation is not None
            assert (snapshot.financial_status != observation.invoice_data["status"]) == expected[
                "financial_status_mutated_by_aco"
            ]
        if "backlog_sends" in expected:
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(Communication)
                    .where(Communication.direction == "outgoing")
                )
                == expected["backlog_sends"]
            )

        observation = db.get(AccountingObservation, observation_id)
        assert observation is not None
        persisted_actions = [db.get(ScheduledAction, ident, populate_existing=True) for ident in queued_ids]
        values = {
            "status": view.status,
            "timely_performance_minor_units": check.timely_minor,
            "performance_status": check.details["performance"],
            "collection_lifecycle": collection.lifecycle,
            "late_performance_minor_units": check.late_minor,
            "promise_hold_active": "active_promise" in collection.hold_reasons,
            "missed_history_preserved": bool(
                db.scalar(
                    select(PromiseEvent).where(
                        PromiseEvent.promise_id == promise.id,
                        PromiseEvent.event == "promise.installment_status",
                        PromiseEvent.details["status"].as_string() == "MISSED",
                    )
                )
            ),
            "pending_outreach_cancelled": bool(persisted_actions)
            and all(item is not None and item.status == "cancelled" for item in persisted_actions),
            "financial_status_mutated_by_aco": snapshot.financial_status
            != observation.invoice_data["status"],
            "backlog_sends": db.scalar(
                select(func.count()).select_from(Communication).where(Communication.direction == "outgoing")
            ),
        }
        boundary_observer.assert_observed(fixture["id"], {key: values[key] for key in expected})


CUTOFF_FIXTURES = [
    case
    for case in json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]
    if case["id"]
    in {
        "promise_end_of_date_cutoff",
        "promise_partial_instalment_not_fulfilled",
        "promise_stale_at_cutoff_keeps_verification_hold",
    }
]


@pytest.mark.parametrize("fixture", CUTOFF_FIXTURES, ids=[case["id"] for case in CUTOFF_FIXTURES])
def test_normative_cutoff_freshness_and_partial_cash(
    monkeypatch: pytest.MonkeyPatch, fixture: dict[str, Any], boundary_observer: BoundaryObserver
) -> None:
    from accounting_support import publish_invoice

    from aco.features.communications.models import Communication
    from aco.features.policies.guards import PolicyGuards
    from aco.features.promises.models import PromiseEvent, PromiseVerification
    from aco.features.promises.performance import verify_invoice_promises

    baseline = json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["domain_baselines"][
        "promise"
    ]
    data, expected = {**baseline, **fixture["input"]}, fixture["expected"]
    confirmed_at = datetime.fromisoformat(data["confirmed_at"].replace("Z", "+00:00"))
    cutoff = datetime.fromisoformat(data["cutoff"].replace("Z", "+00:00"))
    at = datetime.fromisoformat(data.get("now", data["as_of"]).replace("Z", "+00:00"))
    with actor_client("owner") as (client, db, user, tenant):
        invoice = invoices(client, db, tenant)[0]
        ctx = context(tenant, user.id)
        message = communication(db, tenant, [invoice], "SYNTHETIC explicit AUD100 by20November2026.")
        evidence = CommunicationEvidence(
            communication_id=message.id, excerpt=str(message.content["text_body"]), offset=0
        )
        monkeypatch.setattr("aco.features.promises.service.now", lambda: confirmed_at)
        service = PromiseService(db)
        captured = service.record_promise(
            ctx,
            RecordPromise(
                customer_id=invoice.customer_id,
                invoice_ids=[invoice.id],
                evidence=evidence,
                amount=Money(minor_units=data["amount_minor_units"], currency=data["currency"], exponent=2),
                due_date=cutoff.date(),
                timezone=data["recipient_timezone"],
            ),
        )
        promise = service.confirm_promise(
            ctx,
            ConfirmPromise(
                promise_id=captured.id,
                expected_version=captured.version,
                confirmation_evidence=evidence,
                reason="Explicit synthetic fixture confirmation.",
            ),
        )
        assert promise.cutoff_at == cutoff
        db.commit()
        cash = data["verified_cash_minor_units"]
        if cash:
            source_cash(db, invoice, "PARTIAL", cash, cutoff - timedelta(hours=1))
        publish_invoice(
            db,
            tenant,
            invoice.provider_connection_id,
            invoice.external_invoice_id,
            outstanding={"minor_units": 10000 - cash, "currency": "AUD", "exponent": 2},
            status="partially_paid" if cash else "open",
            source_status="PARTIALLY_PAID" if cash else "OPEN",
        )
        read_at = at if data["source_fresh"] else at - timedelta(minutes=16)
        monkeypatch.setattr("aco.integrations.demo_accounting.adapter.now", lambda: read_at)
        observation = actual_read(cast(FastAPI, client.app), ctx, invoice, read_at)
        verify_invoice_promises(db, ctx, observation, clock=FrozenClock(at))
        db.commit()
        view = service.get(ctx, promise.id)
        result = db.scalar(
            select(PromiseVerification)
            .where(PromiseVerification.promise_id == promise.id)
            .order_by(PromiseVerification.created_at.desc(), PromiseVerification.id.desc())
        )
        assert result is not None and view.status == expected["status"]
        if "cutoff_utc" in expected:
            assert view.cutoff_at == datetime.fromisoformat(expected["cutoff_utc"].replace("Z", "+00:00"))
            assert at == view.cutoff_at and (at - view.cutoff_at).total_seconds() == expected["grace_seconds"]
        if "performance_minor_units" in expected:
            assert result.timely_minor == expected["performance_minor_units"]
            assert (view.status == "FULFILLED") == expected["fulfilled"]
        if "hold_reason" in expected:
            decision = PolicyGuards(db, at).operational_status(ctx, invoice.id)
            assert expected["hold_reason"] in decision.reasons
            assert decision.allowed == expected["outbound_allowed"]
            assert (
                bool(
                    db.scalar(
                        select(PromiseEvent).where(
                            PromiseEvent.promise_id == promise.id,
                            PromiseEvent.event == "promise.installment_status",
                            PromiseEvent.details["status"].as_string() == "MISSED",
                        )
                    )
                )
                == expected["claim_missed"]
            )
            assert result.verification_status == "PENDING_REVIEW" and result.details["performance"] is None
        if "backlog_sends" in expected:
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(Communication)
                    .where(Communication.direction == "outgoing")
                )
                == expected["backlog_sends"]
            )

        decision = PolicyGuards(db, at).operational_status(ctx, invoice.id)
        values = {
            "status": view.status,
            "cutoff_utc": view.cutoff_at.isoformat().replace("+00:00", "Z") if view.cutoff_at else None,
            "grace_seconds": int((at - view.cutoff_at).total_seconds()) if view.cutoff_at else None,
            "backlog_sends": db.scalar(
                select(func.count()).select_from(Communication).where(Communication.direction == "outgoing")
            ),
            "performance_minor_units": result.timely_minor,
            "fulfilled": view.status == "FULFILLED",
            "hold_reason": next(
                (reason for reason in decision.reasons if reason == "promise_verification"), None
            ),
            "outbound_allowed": decision.allowed,
            "claim_missed": bool(
                db.scalar(
                    select(PromiseEvent).where(
                        PromiseEvent.promise_id == promise.id,
                        PromiseEvent.event == "promise.installment_status",
                        PromiseEvent.details["status"].as_string() == "MISSED",
                    )
                )
            ),
        }
        boundary_observer.assert_observed(fixture["id"], {key: values[key] for key in expected})
