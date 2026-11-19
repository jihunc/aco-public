"""Normative rejection outcomes through real source records and the maintained worker.

The two after-cutoff fixtures are translated together to a past local-midnight
cutoff so the real database scheduler can execute now. Relative source intervals,
recipient timezone, amounts, refs and every expected result remain unchanged;
exact absolute interval boundaries are exercised separately by the interval fixtures.
"""

import asyncio
import json
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from accounting_support import context, publish_invoice
from boundary_evidence import BoundaryObserver
from case_promise_support import communication, invoices
from fastapi import FastAPI
from identity_support import actor_client
from integration.test_promise_performance import actual_read, source_cash
from sqlalchemy import func, select

from aco.core.types import Money
from aco.features.accounting.models import (
    AccountingObservation,
    CollectionRecord,
    DemoSourceRecord,
    InvoiceSnapshot,
    VerifiedPaymentAllocation,
)
from aco.features.actions.models import ScheduledAction
from aco.features.cases.dto import CommunicationEvidence
from aco.features.cases.service import place_hold
from aco.features.communications.models import Communication
from aco.features.identity.models import now
from aco.features.promises.dto import ConfirmPromise, RecordPromise
from aco.features.promises.models import PromiseEvent, PromiseVerification
from aco.features.promises.service import PromiseService
from aco.features.review.models import CollectionTask
from aco.integrations.demo_email.models import NativeEmailOperation
from aco.worker.app import WorkerRuntime

FIXTURES = [
    case
    for case in json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]
    if case["id"]
    in {
        "promise_reversal_ambiguous_allocation_retains_verification_hold",
        "promise_reversal_currency_ambiguity_retains_review_hold",
    }
]


@pytest.mark.parametrize("fixture", FIXTURES, ids=[case["id"] for case in FIXTURES])
def test_normative_rejected_source_retains_prior_fulfilled_history(
    monkeypatch: pytest.MonkeyPatch, fixture: dict[str, Any], boundary_observer: BoundaryObserver
) -> None:
    data, expected = fixture["input"], fixture["expected"]
    timezone = ZoneInfo("Australia/Melbourne")
    cutoff = datetime.combine(now().astimezone(timezone).date(), time.min, timezone).astimezone(UTC)
    offset = cutoff - datetime.fromisoformat(data["cutoff"].replace("Z", "+00:00"))

    def shifted(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) + offset

    with actor_client("owner") as (client, db, user, tenant):
        app = client.app
        assert isinstance(app, FastAPI)
        invoice = invoices(client, db, tenant)[0]
        invoice_id = invoice.id
        ctx = context(tenant, user.id)
        message = communication(
            db,
            tenant,
            [invoice],
            "SYNTHETIC historical AUD40 documentary commitment; no real customer statement.",
        )
        evidence = CommunicationEvidence(
            communication_id=message.id, excerpt=str(message.content["text_body"]), offset=0
        )
        monkeypatch.setattr("aco.features.promises.service.now", lambda: shifted(data["confirmed_at"]))
        service = PromiseService(db)
        captured = service.record_promise(
            ctx,
            RecordPromise(
                customer_id=invoice.customer_id,
                invoice_ids=[invoice.id],
                evidence=evidence,
                amount=Money(minor_units=data["amount_minor_units"], currency=data["currency"], exponent=2),
                due_date=cutoff.astimezone(timezone).date() - timedelta(days=1),
                timezone=str(timezone),
            ),
        )
        promise = service.confirm_promise(
            ctx,
            ConfirmPromise(
                promise_id=captured.id,
                expected_version=captured.version,
                confirmation_evidence=evidence,
                reason="Explicit historical synthetic fixture documentary confirmation.",
            ),
        )
        assert promise.cutoff_at == cutoff
        place_hold(db, ctx, invoice.id, "active_dispute", "Independent unresolved synthetic dispute.")
        db.commit()
        cash = data["allocations"][0]
        source_cash(db, invoice, cash["id"], cash["minor_units"], shifted(cash["effective_at"]))
        publish_invoice(
            db,
            tenant,
            invoice.provider_connection_id,
            invoice.external_invoice_id,
            outstanding={"minor_units": 5000, "currency": "AUD", "exponent": 2},
            status="partially_paid",
            source_status="PARTIALLY_PAID",
        )
        read_at = now()
        with monkeypatch.context() as reading:
            reading.setattr("aco.integrations.demo_accounting.adapter.now", lambda: read_at)
            prior_observation = actual_read(app, ctx, invoice, read_at)
        db.expire_all()
        prior = service.get(ctx, promise.id)
        assert prior.status == data["prior_status"] == "FULFILLED"
        before_observations = db.scalar(select(func.count()).select_from(AccountingObservation))
        before_allocations = db.scalar(select(func.count()).select_from(VerifiedPaymentAllocation))
        db.commit()
        reversal = data["reversals"][0]
        candidates = [{**item, "kind": "cash", "parent": None} for item in data["allocations"][1:]] + [
            {**reversal, "kind": "reversal", "parent": reversal["allocation"]}
        ]
        for candidate in candidates:
            parent = candidate["parent"]
            db.add(
                DemoSourceRecord(
                    tenant_id=tenant,
                    provider_connection_id=invoice.provider_connection_id,
                    organisation_id=invoice.organisation_id,
                    kind="payment",
                    external_id=candidate["id"] + "/1",
                    revision=1,
                    effective_at=shifted(candidate["effective_at"]),
                    observed_at=now(),
                    payload={
                        "external_payment_id": candidate["id"],
                        "external_allocation_id": "1",
                        "external_invoice_id": invoice.external_invoice_id,
                        "kind": candidate["kind"],
                        "amount": {
                            "minor_units": -candidate["minor_units"]
                            if candidate["kind"] == "reversal"
                            else candidate["minor_units"],
                            "currency": candidate["currency"],
                            "exponent": 2,
                        },
                        "parent_payment_id": parent,
                        "parent_allocation_id": "1" if parent else None,
                    },
                )
            )
        db.commit()
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
            status=data["source_financial_status"],
            source_status="PARTIALLY_PAID",
        )
        db.commit()

        async def work() -> None:
            runtime = WorkerRuntime(app.state.settings)
            try:
                await runtime.app.open_async()
                await runtime.dispatcher.dispatch_outbox(100)
                await runtime.app.run_worker_async(
                    queues=["aco"], concurrency=1, wait=False, install_signal_handlers=False
                )
            finally:
                await runtime.app.close_async()
                await runtime.engine.dispose()

        asyncio.run(work())
        db.expire_all()
        view = service.get(ctx, promise.id)
        result = db.scalar(
            select(PromiseVerification)
            .where(PromiseVerification.promise_id == promise.id)
            .order_by(PromiseVerification.created_at.desc(), PromiseVerification.id.desc())
        )
        collection = db.scalar(select(CollectionRecord).where(CollectionRecord.invoice_id == invoice_id))
        snapshot = db.get(InvoiceSnapshot, invoice_id, populate_existing=True)
        action = db.scalar(select(ScheduledAction).where(ScheduledAction.promise_event_id.is_not(None)))
        assert result is not None and collection is not None and snapshot is not None and action is not None
        assert action.status == "failed" and action.last_error == "VALIDATION_FAILED"
        assert view.status == expected["status"]
        assert (view.status != prior.status) == expected["status_changed"]
        assert result.timely_minor is expected["timely_performance_minor_units"] is None
        assert result.details["performance"] is expected["current_verification_result"] is None
        assert result.verification_status == expected["verification_status"]
        assert (
            any(reason in collection.hold_reasons for reason in ("active_promise", "promise_verification"))
            == expected["promise_hold_active"]
        )
        assert collection.hold_reasons == expected["hold_reasons"]
        events = list(db.scalars(select(PromiseEvent).where(PromiseEvent.promise_id == promise.id)))
        assert (
            sum(
                event.event == "promise.installment_status" and event.details["status"] == "MISSED"
                for event in events
            )
            == expected["new_missed_episode_count"]
        )
        assert (
            any(
                event.event == "promise.installment_status" and event.details["status"] == "FULFILLED"
                for event in events
            )
            == expected["prior_fulfilled_history_preserved"]
        )
        failure = result.details["source_failure"]
        assert isinstance(failure, dict)
        rejected = failure["rejection_evidence"]
        assert isinstance(rejected, dict) and rejected["authenticity"] == "UNVERIFIED"
        primary = rejected["candidate"]
        related = rejected["related_reversal_refs"]
        assert isinstance(primary, dict) and isinstance(related, list)
        refs = [primary, *related]
        reversal_refs = [
            ref for ref in refs if isinstance(ref, dict) and ref["kind"] in {"reversal", "refund"}
        ]
        assert len(reversal_refs) == expected["reversal_evidence_count"]
        assert reversal_refs[0]["external_payment_id"] == reversal["id"]
        assert rejected["related_reversal_refs_truncated"] is False
        if "invalid_currency_candidate_rejected" in expected:
            invalid = db.scalar(
                select(VerifiedPaymentAllocation).where(VerifiedPaymentAllocation.external_payment_id == "A2")
            )
            assert (invalid is None and primary["external_payment_id"] == "A2") == expected[
                "invalid_currency_candidate_rejected"
            ]
        task = db.get(CollectionTask, view.review_task_id)
        assert (task is not None and task.title == "Verify payment promise performance") == expected[
            "review_task_required"
        ]
        assert collection.lifecycle == expected["collection_lifecycle"]
        assert (
            db.scalar(
                select(func.count()).select_from(Communication).where(Communication.direction == "outgoing")
            )
            == expected["backlog_sends"]
        )
        assert (
            db.scalar(select(func.count()).select_from(NativeEmailOperation))
            == expected["actual_provider_submissions"]
        )
        assert db.scalar(select(func.count()).select_from(AccountingObservation)) == before_observations
        assert db.scalar(select(func.count()).select_from(VerifiedPaymentAllocation)) == before_allocations
        assert snapshot.balance_minor == 5000  # Rejected source candidates never become financial truth.

        values: dict[str, object] = {
            "status": view.status,
            "status_changed": view.status != data["prior_status"],
            "current_verification_result": result.details["performance"],
            "timely_performance_minor_units": result.timely_minor,
            "verification_status": result.verification_status,
            "promise_hold_active": any(
                reason in collection.hold_reasons for reason in ("active_promise", "promise_verification")
            ),
            "hold_reasons": collection.hold_reasons,
            "new_missed_episode_count": sum(
                event.event == "promise.installment_status" and event.details["status"] == "MISSED"
                for event in events
            ),
            "prior_fulfilled_history_preserved": any(
                event.event == "promise.installment_status" and event.details["status"] == "FULFILLED"
                for event in events
            ),
            "reversal_evidence_count": len(reversal_refs),
            "invalid_currency_candidate_rejected": rejected["reason"] == "payment_currency_mismatch"
            and primary["external_payment_id"] == "A2"
            and db.scalar(
                select(VerifiedPaymentAllocation).where(VerifiedPaymentAllocation.external_payment_id == "A2")
            )
            is None,
            "review_task_required": task is not None and task.title == "Verify payment promise performance",
            "collection_lifecycle": collection.lifecycle,
            "backlog_sends": db.scalar(
                select(func.count()).select_from(Communication).where(Communication.direction == "outgoing")
            ),
            "actual_provider_submissions": db.scalar(select(func.count()).select_from(NativeEmailOperation)),
        }
        boundary_observer.assert_observed(fixture["id"], {key: values[key] for key in expected})

        from aco.features.promises.performance import verify_invoice_promises

        verify_invoice_promises(db, ctx, prior_observation)
        db.commit()
        assert service.get(ctx, promise.id).verification_status == "PENDING_REVIEW"
