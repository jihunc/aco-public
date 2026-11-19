"""Captured commitments remain evidenced, scoped and separate from money."""

import json
from datetime import date
from pathlib import Path

import pytest
from accounting_support import context
from boundary_evidence import BoundaryObserver
from case_promise_support import communication, invoices
from identity_support import actor_client
from sqlalchemy import select

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.communications.models import Communication
from aco.features.promises.dto import RecordPromise


def promise_command(rows: list[InvoiceSnapshot], msg: Communication, **kw: object) -> RecordPromise:
    from aco.core.types import Money
    from aco.features.cases.dto import CommunicationEvidence
    from aco.features.promises.dto import RecordPromise

    body = msg.content["text_body"]
    assert isinstance(body, str)
    data: dict[str, object] = dict(
        customer_id=rows[0].customer_id,
        invoice_ids=[r.id for r in rows],
        evidence=CommunicationEvidence(communication_id=msg.id, excerpt=body, offset=0),
        amount=Money(minor_units=10000, currency="AUD", exponent=2),
        due_date=date(2026, 11, 20),
        timezone="Australia/Melbourne",
    )
    return RecordPromise.model_validate(data | kw)


def test_promise_holds_only_supported_invoice_scope() -> None:
    from aco.features.accounting.models import CollectionRecord
    from aco.features.promises.service import PromiseService

    with actor_client("owner") as (client, db, user, tenant):
        rows = invoices(client, db, tenant)
        msg = communication(db, tenant, rows[:1], "Synthetic promise: AUD 100 on 2026-11-20.")
        p = PromiseService(db).record_promise(context(tenant, user.id), promise_command(rows[:1], msg))
        db.commit()
        assert p.status == "NEEDS_CONFIRMATION"  # Synthetic unverified author cannot activate itself.
        assert p.cutoff_at is not None
        assert p.needs_confirmation and p.cutoff_at.isoformat() == "2026-11-20T13:00:00+00:00"
        held = db.scalar(select(CollectionRecord).where(CollectionRecord.invoice_id == rows[0].id))
        other = db.scalar(select(CollectionRecord).where(CollectionRecord.invoice_id == rows[1].id))
        assert held is not None and other is not None
        assert "promise_confirmation" in held.hold_reasons
        assert "promise_confirmation" not in other.hold_reasons
        assert rows[0].financial_status != "paid"


@pytest.mark.parametrize(
    "fixture_id",
    [
        "promise_unknown_allocation_needs_confirmation",
        "promise_unknown_scope_blocks_review_without_invented_links",
        "promise_relative_date_missing_timezone",
        "promise_replacement_requires_confirmation",
    ],
)
def test_boundary_promise_confirmation(fixture_id: str, boundary_observer: BoundaryObserver) -> None:
    from aco.features.policies.models import CollectionHold
    from aco.features.promises.dto import ConfirmPromise
    from aco.features.promises.models import CustomerScopeReview, PaymentPromise
    from aco.features.promises.service import PromiseService
    from aco.features.review.models import CollectionTask

    fixture = next(
        c
        for c in json.loads(Path("tests/fixtures/policies/boundaries.json").read_text())["cases"]
        if c["id"] == fixture_id
    )
    with actor_client("owner") as (client, db, user, tenant):
        rows = invoices(client, db, tenant)[:2]
        msg = communication(
            db, tenant, rows, fixture["input"].get("evidence", "Synthetic fixture: new statement ambiguous.")
        )
        svc = PromiseService(db)
        ctx = context(tenant, user.id)
        options: dict[str, object] = {}
        if fixture_id == "promise_unknown_scope_blocks_review_without_invented_links":
            options = dict(
                invoice_ids=[],
                scope="ambiguous",
                amount=None,
                due_date=None,
                timezone=None,
                candidate_invoice_ids=[r.id for r in rows],
            )
        if fixture_id == "promise_relative_date_missing_timezone":
            options = dict(invoice_ids=[rows[0].id], due_date=None, timezone=None)
        if fixture_id == "promise_replacement_requires_confirmation":
            oldmsg = communication(db, tenant, rows[:1], "Synthetic old commitment: AUD 100 on 2026-11-20.")
            old = svc.record_promise(ctx, promise_command(rows[:1], oldmsg))
            db.commit()
            old = svc.confirm_promise(
                ctx,
                ConfirmPromise(
                    promise_id=old.id,
                    expected_version=old.version,
                    confirmation_evidence=promise_command(rows[:1], oldmsg).evidence,
                    reason="Owner independently confirms sender authority and exact original commitment",
                ),
            )
            db.commit()
            assert old.status == "ACTIVE"
            options = dict(invoice_ids=[rows[0].id], amount=None, due_date=None, replaces_promise_id=old.id)
        result = svc.record_promise(ctx, promise_command(rows, msg, **options))
        db.commit()
        expected = fixture["expected"]
        actual: dict[str, object] = {"status": result.status}
        assert result.status == expected.get("status", expected.get("new_status"))
        if fixture_id == "promise_unknown_allocation_needs_confirmation":
            assert [link.amount for link in result.invoice_links] == [None, None]
            assert {link.invoice_id for link in result.invoice_links} == {r.id for r in rows}
            holds = list(
                db.scalars(
                    select(CollectionHold).where(
                        CollectionHold.id.in_([link.hold_id for link in result.invoice_links])
                    )
                )
            )
            assert all(h.reason == expected["hold_reason"] for h in holds)
            task = db.get(CollectionTask, result.review_task_id)
            assert task is not None and task.due_at is not None
            assert (task.due_at - result.created_at).total_seconds() == expected["review_due_seconds"]
            aliases = dict(zip((row.id for row in rows), fixture["input"]["invoice_ids"], strict=True))
            actual.update(
                allocations=None
                if all(link.amount is None for link in result.invoice_links)
                else [link.amount for link in result.invoice_links],
                hold_invoice_ids=sorted(aliases[hold.invoice_id] for hold in holds),
                hold_reason=holds[0].reason if holds and len({hold.reason for hold in holds}) == 1 else None,
                review_due_seconds=int((task.due_at - result.created_at).total_seconds()),
            )
        elif fixture_id == "promise_unknown_scope_blocks_review_without_invented_links":
            assert (
                result.invoice_links
                == expected["invoice_allocations"]
                == expected["promise_hold_invoice_ids"]
            )
            block = db.scalar(select(CustomerScopeReview).where(CustomerScopeReview.promise_id == result.id))
            assert block is not None
            assert block.reason == expected["customer_block"] and block.released_at is None
            task = db.get(CollectionTask, result.review_task_id)
            assert task is not None and task.due_at is not None
            assert (task.due_at - result.created_at).total_seconds() == expected["review_due_seconds"]
            assert block.expires_at is None and expected["auto_release_after_deadline"] is False
            actual.update(
                invoice_allocations=[link.amount for link in result.invoice_links],
                promise_hold_invoice_ids=[
                    str(link.invoice_id) for link in result.invoice_links if link.hold_id
                ],
                customer_block=block.reason,
                review_due_seconds=int((task.due_at - result.created_at).total_seconds()),
                auto_release_after_deadline=block.expires_at is not None,
            )
        elif fixture_id == "promise_relative_date_missing_timezone":
            assert result.due_date is None and result.cutoff_at is None and expected["invented_date"] is False
            assert [link.invoice_id for link in result.invoice_links] == [rows[0].id]
            holds = list(
                db.scalars(
                    select(CollectionHold).where(
                        CollectionHold.id.in_([link.hold_id for link in result.invoice_links])
                    )
                )
            )
            aliases = {rows[0].id: fixture["input"]["invoice_ids"][0]}
            actual.update(
                hold_invoice_ids=[aliases[hold.invoice_id] for hold in holds],
                invented_date=result.cutoff_at is not None,
            )
        else:
            prior = db.get(PaymentPromise, old.id)
            assert prior is not None
            assert prior.status == expected["prior_status"]
            assert (
                prior.source_text == oldmsg.content["text_body"]
                and expected["prior_history_deleted"] is False
            )
            actual = {
                "prior_status": prior.status,
                "new_status": result.status,
                "prior_history_deleted": prior.source_text != oldmsg.content["text_body"],
            }
        boundary_observer.assert_observed(fixture_id, actual)


def test_confirmed_replacement_preserves_previous_and_other_holds() -> None:
    from aco.features.promises.dto import ConfirmPromise
    from aco.features.promises.models import PaymentPromise
    from aco.features.promises.service import PromiseService

    with actor_client("owner") as (client, db, user, tenant):
        rows = invoices(client, db, tenant)[:1]
        msg = communication(db, tenant, rows, "Synthetic promise AUD 100 on 2026-11-20.")
        svc = PromiseService(db)
        ctx = context(tenant, user.id)
        old = svc.record_promise(ctx, promise_command(rows, msg))
        db.commit()
        old = svc.confirm_promise(
            ctx,
            ConfirmPromise(
                promise_id=old.id,
                expected_version=old.version,
                confirmation_evidence=promise_command(rows, msg).evidence,
                reason="Owner confirmed authority and exact commitment",
            ),
        )
        db.commit()
        newer = communication(db, tenant, rows, "Synthetic revised promise AUD 100 on 2026-11-21.")
        new = svc.record_promise(
            ctx, promise_command(rows, newer, due_date=date(2026, 11, 21), replaces_promise_id=old.id)
        )
        db.commit()
        new = svc.confirm_promise(
            ctx,
            ConfirmPromise(
                promise_id=new.id,
                expected_version=new.version,
                replacement_expected_version=old.version,
                confirmation_evidence=promise_command(rows, newer).evidence,
                reason="Owner verified explicit compatible amendment",
            ),
        )
        db.commit()
        prior = db.get(PaymentPromise, old.id)
        assert prior is not None and prior.status == "SUPERSEDED"
        assert new.status == "ACTIVE" and new.confirmed_at and new.replaces_promise_id == old.id
        assert prior.source_text == msg.content["text_body"]
        replay = svc.record_promise(
            ctx, promise_command(rows, newer, due_date=date(2026, 11, 21), replaces_promise_id=old.id)
        )
        assert replay.id == new.id and replay.version == new.version and replay.status == "ACTIVE"


def test_promise_response_money_keeps_exact_large_integer() -> None:
    from aco.core.types import Money
    from aco.features.promises.service import PromiseService

    with actor_client("owner") as (client, db, user, tenant):
        rows = invoices(client, db, tenant)[:1]
        msg = communication(db, tenant, rows, "Synthetic promise amount 10000000000000001 minor units.")
        result = PromiseService(db).record_promise(
            context(tenant, user.id),
            promise_command(
                rows, msg, amount=Money(minor_units=10000000000000001, currency="AUD", exponent=2)
            ),
        )
        wire = result.model_dump(mode="json")
        assert wire["amount"]["minor_units"] == "10000000000000001"
        assert wire["invoice_links"][0]["amount"]["minor_units"] == "10000000000000001"
        assert wire["installments"][0]["allocations"][0]["amount"]["minor_units"] == "10000000000000001"


def test_partial_installments_preserve_dispute_hold_and_no_accounting_mutation() -> None:
    from integration.test_case_lifecycle import command

    from aco.core.types import Money
    from aco.features.accounting.models import CollectionRecord
    from aco.features.cases.service import CaseService
    from aco.features.policies.models import CollectionHold
    from aco.features.promises.dto import CancelPromise, ConfirmPromise, PromiseInstallmentCommand
    from aco.features.promises.service import PromiseService

    with actor_client("owner") as (client, db, user, tenant):
        rows = invoices(client, db, tenant)[:1]
        msg = communication(db, tenant, rows, "Synthetic dispute and instalments: AUD 40 then AUD 60.")
        ctx = context(tenant, user.id)
        CaseService(db).upsert_case(ctx, command(rows, msg))
        db.commit()
        before = (rows[0].balance_minor, rows[0].financial_status, rows[0].source_revision)
        svc = PromiseService(db)
        record = promise_command(
            rows,
            msg,
            installments=[
                PromiseInstallmentCommand(
                    due_date=date(2026, 11, 20), amount=Money(minor_units=4000, currency="AUD", exponent=2)
                ),
                PromiseInstallmentCommand(
                    due_date=date(2026, 11, 21), amount=Money(minor_units=6000, currency="AUD", exponent=2)
                ),
            ],
        )
        p = svc.record_promise(ctx, record)
        db.commit()
        p = svc.confirm_promise(
            ctx,
            ConfirmPromise(
                promise_id=p.id,
                expected_version=p.version,
                confirmation_evidence=record.evidence,
                reason="Owner validates exact instalment commitment and sender authority",
            ),
        )
        db.commit()
        assert [i.amount.minor_units for i in p.installments] == [4000, 6000]
        plans = svc.prepare_promise_checks(ctx, p.id)
        assert [x.due_at.isoformat() for x in plans] == [
            "2026-11-20T13:00:00+00:00",
            "2026-11-21T13:00:00+00:00",
        ]
        assert all(
            x.status == "QUEUED" and x.invoice_ids == [rows[0].id] and len(x.action_ids) == 1 for x in plans
        )
        assert all(
            h.expires_at is None
            for h in db.scalars(select(CollectionHold).where(CollectionHold.reason == "active_promise"))
        )
        svc.cancel_promise(
            ctx,
            CancelPromise(
                promise_id=p.id,
                expected_version=p.version,
                evidence=record.evidence,
                reason="Authorized documented cancellation",
            ),
        )
        db.commit()
        db.expire_all()
        collection = db.scalar(select(CollectionRecord).where(CollectionRecord.invoice_id == rows[0].id))
        assert collection is not None
        hold_reasons = collection.hold_reasons
        assert "active_dispute" in hold_reasons and "active_promise" not in hold_reasons
        assert (rows[0].balance_minor, rows[0].financial_status, rows[0].source_revision) == before


def test_confirm_promise_requires_current_resolution_authority_and_exact_version() -> None:
    from fastapi import HTTPException

    from aco.features.identity.models import Membership
    from aco.features.promises.dto import ConfirmPromise
    from aco.features.promises.service import PromiseService

    with actor_client("owner") as (client, db, user, tenant):
        rows = invoices(client, db, tenant)[:1]
        msg = communication(db, tenant, rows, "Synthetic promise: AUD100 Friday.")
        ctx = context(tenant, user.id)
        svc = PromiseService(db)
        cmd = promise_command(rows, msg)
        p = svc.record_promise(ctx, cmd)
        db.commit()
        confirm = ConfirmPromise(
            promise_id=p.id,
            expected_version=p.version,
            confirmation_evidence=cmd.evidence,
            reason="Independently verified",
        )
        membership = db.scalar(
            select(Membership).where(Membership.tenant_id == tenant, Membership.user_id == user.id)
        )
        assert membership is not None
        membership.role = "operator"
        db.commit()
        with pytest.raises(HTTPException) as denied:
            svc.confirm_promise(ctx, confirm)
        assert denied.value.status_code == 403
        membership.role = "owner"
        db.commit()
        with pytest.raises(HTTPException) as stale:
            svc.confirm_promise(ctx, confirm.model_copy(update={"expected_version": p.version + 1}))
        assert stale.value.status_code == 409
        assert svc.get(ctx, p.id).status == "NEEDS_CONFIRMATION"


@pytest.mark.parametrize("decision", ["confirm", "cancel"])
def test_promise_decision_closes_its_confirmation_task(decision: str) -> None:
    from aco.features.promises.dto import CancelPromise, ConfirmPromise
    from aco.features.promises.service import PromiseService
    from aco.features.review.models import CollectionTask

    with actor_client("owner") as (client, db, user, tenant):
        rows = invoices(client, db, tenant)[:1]
        msg = communication(db, tenant, rows, "Synthetic promise: AUD100 Friday.")
        ctx = context(tenant, user.id)
        svc = PromiseService(db)
        cmd = promise_command(rows, msg)
        p = svc.record_promise(ctx, cmd)
        db.commit()
        assert p.review_task_id is not None
        task = db.get(CollectionTask, p.review_task_id)
        assert task is not None and task.status == "open"
        version = task.version
        if decision == "confirm":
            svc.confirm_promise(
                ctx,
                ConfirmPromise(
                    promise_id=p.id,
                    expected_version=p.version,
                    confirmation_evidence=cmd.evidence,
                    reason="Independently verified",
                ),
            )
        else:
            svc.cancel_promise(
                ctx,
                CancelPromise(
                    promise_id=p.id,
                    expected_version=p.version,
                    reason="Customer withdrew",
                    evidence=cmd.evidence,
                ),
            )
        db.commit()
        db.expire_all()
        task = db.get(CollectionTask, p.review_task_id)
        assert task is not None and task.status == "done" and task.version == version + 1


def test_confirming_a_clarification_closes_the_superseded_confirmation_task() -> None:
    from aco.features.promises.dto import ConfirmPromise
    from aco.features.promises.service import PromiseService
    from aco.features.review.models import CollectionTask

    with actor_client("owner") as (client, db, user, tenant):
        rows = invoices(client, db, tenant)[:1]
        msg = communication(db, tenant, rows, "Synthetic unresolved promise: I will pay soon.")
        svc = PromiseService(db)
        ctx = context(tenant, user.id)
        old = svc.record_promise(ctx, promise_command(rows, msg, due_date=None, timezone=None))
        db.commit()
        newer = communication(db, tenant, rows, "Synthetic clarified promise AUD 100 on 2026-11-21.")
        cmd = promise_command(
            rows,
            newer,
            due_date=date(2026, 11, 21),
            replaces_promise_id=old.id,
            replacement_expected_version=old.version,
        )
        new = svc.record_promise(ctx, cmd)
        db.commit()
        svc.confirm_promise(
            ctx,
            ConfirmPromise(
                promise_id=new.id,
                expected_version=new.version,
                replacement_expected_version=old.version,
                confirmation_evidence=cmd.evidence,
                reason="Owner verified the clarified commitment",
            ),
        )
        db.commit()
        db.expire_all()
        for promise in (old, new):
            assert promise.review_task_id is not None
            task = db.get(CollectionTask, promise.review_task_id)
            assert task is not None and task.status == "done"


def test_http_promises_source_provenance_and_csrf() -> None:
    with actor_client("owner") as (client, db, user, tenant):
        rows = invoices(client, db, tenant)[:1]
        msg = communication(db, tenant, rows, "Synthetic promise: AUD 100 November20.")
        path = f"/api/workspaces/{tenant}/promises"
        payload = promise_command(rows, msg).model_dump(mode="json")
        assert client.post(path, json=payload, headers={"x-csrf-token": "invalid"}).status_code == 403
        response = client.post(path, json=payload)
        assert response.status_code == 201, response.text
        p = response.json()
        assert (
            p["source_communication_id"] == str(msg.id)
            and p["source_state"] == "synthetic_fixture"
            and p["synthetic"]
        )
        assert p["amount"]["minor_units"] == "10000"
        detail = client.get(path + "/" + p["id"])
        assert detail.status_code == 200 and detail.json() == p
        assert client.get(path, params={"invoice_id": str(rows[0].id)}).json()["items"][0]["id"] == p["id"]
