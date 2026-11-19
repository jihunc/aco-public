"""Pending terms gain a retained-source replacement, never a silent rewrite."""

from uuid import UUID

import pytest
from accounting_support import context
from case_promise_support import communication, invoices
from identity_support import actor_client
from integration.test_promise_lifecycle import promise_command
from sqlalchemy import select

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.cases.service import place_hold
from aco.features.policies.models import CollectionHold
from aco.features.promises.models import CustomerScopeReview


@pytest.mark.parametrize("missing", ["date", "allocation", "scope"])
def test_pending_clarification_preserves_source_and_moves_only_its_holds(missing: str) -> None:
    with actor_client("owner") as (client, db, user, tenant):
        rows = invoices(client, db, tenant)[:2]
        original = communication(db, tenant, rows, "Synthetic unresolved promise: I will pay those soon.")
        options: dict[str, object] = {}
        if missing == "date":
            options = {"due_date": None, "timezone": None}
        elif missing == "scope":
            options = {
                "invoice_ids": [],
                "candidate_invoice_ids": [r.id for r in rows],
                "scope": "ambiguous",
                "amount": None,
                "due_date": None,
                "timezone": None,
            }
        old_command = promise_command(rows, original, **options)
        base = f"/api/workspaces/{tenant}/promises"
        response = client.post(base, json=old_command.model_dump(mode="json"))
        assert response.status_code == 201, response.text
        old = response.json()
        assert old["status"] == "NEEDS_CONFIRMATION"
        db.rollback()
        unrelated = place_hold(db, context(tenant, user.id), rows[0].id, "active_dispute", "Separate case")
        db.commit()
        original_financial = [(r.id, r.source_revision, r.balance_minor, r.financial_status) for r in rows]
        clarified = communication(
            db,
            tenant,
            rows,
            "Synthetic clarification: AUD 60 INV-101 and AUD 40 INV-102 on 2026-11-20 Australia/Melbourne.",
        )
        command = promise_command(rows, clarified).model_dump(mode="json")
        command.update(
            {
                "replaces_promise_id": old["id"],
                "replacement_expected_version": old["version"],
                "allocations": [
                    {
                        "invoice_id": str(row.id),
                        "amount": {"minor_units": minor, "currency": "AUD", "exponent": 2},
                    }
                    for row, minor in zip(rows, [6000, 4000], strict=True)
                ],
            }
        )
        db.rollback()
        response = client.post(base, json=command)
        assert response.status_code == 201, response.text
        replacement = response.json()
        assert replacement["status"] == "NEEDS_CONFIRMATION"
        assert client.get(f"{base}/{old['id']}").json()["status"] == "NEEDS_CONFIRMATION"
        confirmation = {
            "promise_id": replacement["id"],
            "expected_version": replacement["version"],
            "replacement_expected_version": old["version"],
            "confirmation_evidence": command["evidence"],
            "reason": "Current human reviewed original and new explicit clarification source.",
        }
        stale = client.post(
            f"{base}/{replacement['id']}/confirm",
            json=confirmation | {"replacement_expected_version": old["version"] + 1},
        )
        assert stale.status_code == 409, stale.text
        response = client.post(f"{base}/{replacement['id']}/confirm", json=confirmation)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "ACTIVE"
        prior = client.get(f"{base}/{old['id']}").json()
        assert prior["status"] == "SUPERSEDED" and prior["superseded_by_id"] == replacement["id"]
        assert (
            prior["source_communication_id"] == str(original.id)
            and prior["source_text"] == old["source_text"]
        )
        assert prior["amount"] == old["amount"] and prior["invoice_links"] == old["invoice_links"]
        assert client.get(f"{base}/{replacement['id']}/check-plans").json()[0]["action_ids"]
        db.rollback()
        unrelated_hold = db.get(CollectionHold, unrelated)
        assert unrelated_hold is not None and unrelated_hold.released_at is None
        for link in old["invoice_links"]:
            hold = db.get(CollectionHold, UUID(link["hold_id"]))
            assert hold is not None and hold.released_at is not None
        block = db.scalar(
            select(CustomerScopeReview).where(CustomerScopeReview.promise_id == UUID(old["id"]))
        )
        if missing == "scope":
            assert block is not None and block.released_at is not None
            for key in block.hold_ids:
                scope_hold = db.get(CollectionHold, UUID(key))
                assert scope_hold is not None and scope_hold.released_at is not None
        actual = [db.get(InvoiceSnapshot, key, populate_existing=True) for key, *_ in original_financial]
        assert [
            (r.id, r.source_revision, r.balance_minor, r.financial_status) for r in actual if r
        ] == original_financial


def test_pending_clarification_refuses_changed_known_terms_source_scope_and_authority() -> None:
    from fastapi import FastAPI
    from identity_support import add_actor

    from aco.features.identity.models import Membership

    with actor_client("owner") as (client, db, user, tenant):
        rows = invoices(client, db, tenant)[:2]
        source = communication(db, tenant, rows[:1], "Synthetic AUD 100 promise; date unresolved.")
        base = f"/api/workspaces/{tenant}/promises"
        old_command = promise_command(rows[:1], source, due_date=None).model_dump(mode="json")
        response = client.post(base, json=old_command)
        assert response.status_code == 201, response.text
        old = response.json()
        next_source = communication(
            db, tenant, rows, "Synthetic clarification AUD 100 on 2026-11-20 Australia/Melbourne."
        )
        command = promise_command(rows[:1], next_source).model_dump(mode="json") | {
            "replaces_promise_id": old["id"],
            "replacement_expected_version": old["version"],
        }
        foreign = db.scalar(
            select(InvoiceSnapshot).where(
                InvoiceSnapshot.tenant_id == tenant, InvoiceSnapshot.customer_id != rows[0].customer_id
            )
        )
        assert foreign is not None
        foreign_source = communication(db, tenant, [foreign], "Synthetic unrelated customer statement.")
        foreign_evidence = promise_command([foreign], foreign_source).model_dump(mode="json")["evidence"]
        db.rollback()
        assert client.post(base, json=command | {"evidence": foreign_evidence}).status_code == 403
        changes: list[tuple[dict[str, object], str]] = [
            ({"replacement_expected_version": None}, "STALE_PRIOR_PROMISE"),
            ({"replacement_expected_version": 999}, "STALE_PRIOR_PROMISE"),
            ({"evidence": old_command["evidence"]}, "PROMISE_CLARIFICATION_NEW_SOURCE_REQUIRED"),
            ({"invoice_ids": [str(rows[1].id)]}, "INCOMPATIBLE_PROMISE_CLARIFICATION"),
            (
                {"amount": {"minor_units": 9000, "currency": "AUD", "exponent": 2}},
                "PROMISE_CLARIFICATION_CONTRADICTS_KNOWN_TERMS",
            ),
            ({"timezone": "UTC"}, "PROMISE_CLARIFICATION_CONTRADICTS_KNOWN_TERMS"),
        ]
        for change, code in changes:
            db.rollback()
            refused = client.post(base, json=command | change)
            assert refused.status_code == 409 and refused.json()["detail"] == code, refused.text
        bad_source = dict(command["evidence"], excerpt="not present in retained original")
        assert client.post(base, json=command | {"evidence": bad_source}).status_code == 422
        csrf = client.post(base, json=command, headers={"x-csrf-token": "incorrect"})
        assert csrf.status_code == 403
        app = client.app
        assert isinstance(app, FastAPI)
        viewer, viewer_client = add_actor(db, app, "promise-viewer@synthetic.invalid")
        db.add(Membership(tenant_id=tenant, user_id=viewer.id, role="viewer"))
        db.commit()
        assert viewer_client.post(base, json=command).status_code == 403
        response = client.post(base, json=command)
        assert response.status_code == 201, response.text
        saved = response.json()
        assert client.post(base, json=command).json()["id"] == saved["id"]
        confirmation = {
            "promise_id": saved["id"],
            "expected_version": saved["version"],
            "replacement_expected_version": old["version"],
            "confirmation_evidence": command["evidence"],
            "reason": "Human reviewed current original and clarified terms.",
        }
        # A different exact message cannot replace the captured clarification review.
        refused = client.post(
            f"{base}/{saved['id']}/confirm",
            json=confirmation | {"confirmation_evidence": old_command["evidence"]},
        )
        assert (
            refused.status_code == 409 and refused.json()["detail"] == "PROMISE_CLARIFICATION_SOURCE_CHANGED"
        )
        assert viewer_client.post(f"{base}/{saved['id']}/confirm", json=confirmation).status_code == 403
        cancelled = client.post(
            f"{base}/{old['id']}/cancel",
            json={
                "promise_id": old["id"],
                "expected_version": old["version"],
                "reason": "Independent current disposition before replacement confirmation",
                "evidence": old_command["evidence"],
            },
        )
        assert cancelled.status_code == 200, cancelled.text
        refused = client.post(f"{base}/{saved['id']}/confirm", json=confirmation)
        assert refused.status_code == 409 and refused.json()["detail"] == "STALE_PRIOR_PROMISE"
        assert client.get(f"{base}/{saved['id']}").json()["status"] == "NEEDS_CONFIRMATION"


def test_actual_signed_source_worker_pending_promise_can_be_human_clarified() -> None:
    import asyncio
    from datetime import timedelta

    from evaluations.test_collection_agent import promise, proposal, run_sdk
    from ingress_support import ingress_world

    from aco.features.communications.models import Communication, EmailIngressReceipt
    from aco.features.identity.models import now
    from aco.features.promises.models import PaymentPromise

    with ingress_world() as lane:
        world, db = lane.world, lane.world.store
        invoice = db.get(InvoiceSnapshot, world.invoice_id)
        assert invoice is not None
        original = lane.message(body="Synthetic INV-101 promise: AUD 100 next Friday.")
        day = now().date() + timedelta(days=7)
        clarification = lane.message(
            body=f"Synthetic clarification INV-101: AUD 100 on {day.isoformat()} UTC."
        )
        db.rollback()

        async def capture() -> tuple[UUID, UUID]:
            first = await lane.deliver(original)
            second = await lane.deliver(clarification)
            await lane.work(None)
            return first, second

        receipts = asyncio.run(capture())
        messages: list[Communication] = []
        db.rollback()
        for key in receipts:
            receipt = db.get(EmailIngressReceipt, key)
            assert receipt is not None and receipt.communication_id is not None
            message = db.get(Communication, receipt.communication_id)
            assert message is not None and message.source_state == "stored"
            messages.append(message)
        command = promise(invoice, messages[0], due_date=None, timezone=None)
        saved = run_sdk(
            world,
            [invoice],
            [proposal(invoice, "record_promise", promise_command=command)],
            extra_reads=[("get_communication_history", invoice.id)],
        )
        assert saved["status"] == "completed", saved
        db.rollback()
        pending = db.scalar(select(PaymentPromise))
        assert pending is not None and pending.status == "NEEDS_CONFIRMATION"
        base = f"/api/workspaces/{world.ctx.tenant_id}/promises"
        clarified = promise(invoice, messages[1], due_date=day.isoformat(), timezone="UTC") | {
            "replaces_promise_id": str(pending.id),
            "replacement_expected_version": pending.version,
        }
        old_id, old_version = pending.id, pending.version
        db.rollback()
        response = world.client.post(base, json=clarified)
        assert response.status_code == 201, response.text
        new = response.json()
        assert new["status"] == "NEEDS_CONFIRMATION"
        response = world.client.post(
            f"{base}/{new['id']}/confirm",
            json={
                "promise_id": new["id"],
                "expected_version": new["version"],
                "replacement_expected_version": old_version,
                "confirmation_evidence": clarified["evidence"],
                "reason": "Human resolved the original date using a separately retained explicit reply.",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "ACTIVE"
        assert world.client.get(f"{base}/{old_id}").json()["status"] == "SUPERSEDED"
        db.rollback()
        assert len(list(db.scalars(select(Communication)))) == 2
        retained_invoice = db.get(InvoiceSnapshot, invoice.id, populate_existing=True)
        assert retained_invoice is not None and retained_invoice.financial_status != "paid"


@pytest.mark.parametrize("change_ancestor", [False, True])
def test_pending_amendment_clarification_rechecks_and_supersedes_active_ancestor(
    change_ancestor: bool,
) -> None:
    with actor_client("owner") as (client, db, user, tenant):
        rows = invoices(client, db, tenant)[:1]
        base = f"/api/workspaces/{tenant}/promises"
        original = communication(
            db, tenant, rows, "Synthetic original AUD 100 on 2026-11-20 Australia/Melbourne."
        )
        original_command = promise_command(rows, original).model_dump(mode="json")
        first = client.post(base, json=original_command).json()
        reviewed = client.post(
            f"{base}/{first['id']}/confirm",
            json={
                "promise_id": first["id"],
                "expected_version": first["version"],
                "confirmation_evidence": original_command["evidence"],
                "reason": "Human reviews original terms.",
            },
        )
        assert reviewed.status_code == 200, reviewed.text
        active = reviewed.json()
        statement = communication(db, tenant, rows, "Synthetic amendment AUD 100; revised date to follow.")
        amendment_command = promise_command(
            rows, statement, due_date=None, replaces_promise_id=active["id"]
        ).model_dump(mode="json")
        amended = client.post(base, json=amendment_command)
        assert amended.status_code == 201, amended.text
        pending = amended.json()
        statement2 = communication(
            db, tenant, rows, "Synthetic revised date clarified: AUD 100 on 2026-11-21 Australia/Melbourne."
        )
        clarified_command = promise_command(rows, statement2).model_dump(mode="json") | {
            "due_date": "2026-11-21",
            "replaces_promise_id": pending["id"],
            "replacement_expected_version": pending["version"],
        }
        response = client.post(base, json=clarified_command)
        assert response.status_code == 201, response.text
        new = response.json()
        confirmation = {
            "promise_id": new["id"],
            "expected_version": new["version"],
            "replacement_expected_version": pending["version"],
            "active_predecessor_expected_version": active["version"],
            "confirmation_evidence": clarified_command["evidence"],
            "reason": "Reviewed full original and pending lineage.",
        }
        if change_ancestor:
            result = client.post(
                f"{base}/{active['id']}/cancel",
                json={
                    "promise_id": active["id"],
                    "expected_version": active["version"],
                    "evidence": original_command["evidence"],
                    "reason": "Independent ancestor disposition.",
                },
            )
            assert result.status_code == 200, result.text
        response = client.post(f"{base}/{new['id']}/confirm", json=confirmation)
        assert response.status_code == (409 if change_ancestor else 200), response.text
        for previous in [active, pending]:
            current = client.get(f"{base}/{previous['id']}").json()
            if not change_ancestor:
                assert current["status"] == "SUPERSEDED" and current["superseded_by_id"] == new["id"]
            else:
                assert current["status"] == ("CANCELLED" if previous is active else "NEEDS_CONFIRMATION")
        assert client.get(f"{base}/{new['id']}").json()["status"] == (
            "NEEDS_CONFIRMATION" if change_ancestor else "ACTIVE"
        )
