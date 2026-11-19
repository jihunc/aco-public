"""Current-state continuation, exposure fences and capacity release on real PostgreSQL."""

import hashlib
import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from accounting_support import apply, publish_invoice
from fastapi import HTTPException
from integration.test_policy_authority import setup_active
from policy_support import configure_recipients, policy_world, save
from sqlalchemy import select

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.actions.models import ScheduledAction
from aco.features.identity.models import now
from aco.features.policies.guards import PolicyGuards
from aco.features.policies.models import ContactReservation, PolicyOccurrence
from aco.features.policies.service import PolicyService


def test_pending_occurrences_select_one_after_hold_policy_and_source_change() -> None:
    with policy_world() as (client, store, ctx, invoices, saved):
        configure_recipients(client, store, ctx)
        invoice = next(i for i in invoices if i.financial_status == "open" and i.balance_minor > 0)
        invoice_id, connection, external = (
            invoice.id,
            invoice.provider_connection_id,
            invoice.external_invoice_id,
        )
        rules = [
            {**saved["definition"]["rules"][0], "id": str(uuid4()), "offset_days": d} for d in (-30, -20, -10)
        ]
        config = {**saved["definition"], "rules": rules}
        # Persist historical no-outreach reconsideration fixtures. No submission is fabricated.
        for rule in rules:
            store.add(
                PolicyOccurrence(
                    tenant_id=ctx.tenant_id,
                    invoice_id=invoice.id,
                    policy_id=UUID(saved["policy_id"]),
                    rule_id=UUID(rule["id"]),
                    due_date_anchor=invoice.due_date,
                    operation_key=hashlib.sha256(
                        json.dumps(
                            [
                                str(ctx.tenant_id),
                                str(invoice.provider_connection_id),
                                str(invoice.id),
                                saved["policy_id"],
                                rule["id"],
                                invoice.due_date.isoformat(),
                            ]
                        ).encode()
                    ).hexdigest(),
                    state="pending_recompute",
                    reconsider_after=now(),
                    last_reason="SYNTHETIC_VERIFIED_NO_OUTREACH_HISTORY",
                )
            )
        PolicyService(store).place_hold(
            ctx, invoice.id, "active_promise", "Synthetic promise expiry", now() + timedelta(hours=1)
        )
        store.commit()
        changed = save(
            client,
            ctx.tenant_id,
            policy_id=saved["policy_id"],
            name=saved["name"],
            expected_version=saved["version"],
            definition=config,
            activate=True,
            native_reminders_disabled=True,
            configuration_reviewed=True,
        )
        assert not list(
            store.scalars(select(ScheduledAction).where(ScheduledAction.invoice_id == invoice_id))
        )
        before = store.get(InvoiceSnapshot, invoice_id)
        assert before
        revision = before.source_revision
        event = publish_invoice(store, ctx.tenant_id, connection, external, source_status="OPEN_REVIEWED")
        apply(client.app, ctx, event)
        store.expire_all()
        current = store.get(InvoiceSnapshot, invoice_id)
        assert current and current.source_revision != revision
        at = now() + timedelta(hours=2)
        PolicyService(store, at).release_expired_holds(ctx, invoice_id, at)
        store.flush()
        queued = list(
            store.scalars(
                select(ScheduledAction).where(
                    ScheduledAction.invoice_id == invoice_id, ScheduledAction.status == "queued"
                )
            )
        )
        assert len(queued) == 1 and queued[0].due_at > at
        assert queued[0].command["rule_id"] == rules[-1]["id"]
        assert str(queued[0].policy_version_id) == changed["id"]
        assert queued[0].source_revision == current.source_revision
        pending = list(
            store.scalars(
                select(PolicyOccurrence).where(
                    PolicyOccurrence.invoice_id == invoice_id, PolicyOccurrence.state == "superseded"
                )
            )
        )
        assert {str(p.rule_id) for p in pending} == {r["id"] for r in rules[:2]}
        # Repeated selection cannot resurrect either older rule or duplicate the selected grant.
        PolicyService(store, at).recompute_next_action(ctx, invoice_id, at)
        assert (
            len(
                list(
                    store.scalars(
                        select(ScheduledAction).where(
                            ScheduledAction.invoice_id == invoice_id, ScheduledAction.status == "queued"
                        )
                    )
                )
            )
            == 1
        )


@pytest.mark.parametrize("state", ["reserved", "inflight", "accepted", "unknown_outcome"])
def test_contact_exposure_fences_replacement_across_policy_versions(state: str) -> None:
    with policy_world() as (client, store, ctx, invoices, saved):
        saved = setup_active(client, store, ctx, saved)
        action = store.scalar(select(ScheduledAction).where(ScheduledAction.tenant_id == ctx.tenant_id))
        assert action
        invoice = store.get(InvoiceSnapshot, action.invoice_id)
        assert invoice
        original = (action.id, action.operation_key, action.policy_version_id, action.occurrence_id)
        store.add(
            ContactReservation(
                tenant_id=ctx.tenant_id,
                invoice_id=invoice.id,
                customer_id=invoice.customer_id,
                operation_id=uuid4(),
                destination="reviewed@example.test",
                channel="email",
                status=state,
                reserved_at=now(),
                action_id=action.id,
                occurrence_id=action.occurrence_id,
                evidence={"origin": "synthetic exposure boundary"},
            )
        )
        store.commit()
        changed = {**saved["definition"], "rules": [{**saved["definition"]["rules"][0], "offset_days": 121}]}
        save(
            client,
            ctx.tenant_id,
            policy_id=saved["policy_id"],
            name=saved["name"],
            expected_version=saved["version"],
            definition=changed,
            activate=True,
            native_reminders_disabled=True,
            configuration_reviewed=True,
        )
        store.expire_all()
        assert (action.id, action.operation_key, action.policy_version_id, action.occurrence_id) == original
        assert (
            len(
                list(
                    store.scalars(
                        select(ScheduledAction).where(ScheduledAction.occurrence_id == action.occurrence_id)
                    )
                )
            )
            == 1
        )


def test_normalized_phone_capacity_release_requires_stored_no_effect() -> None:
    with policy_world() as (client, store, ctx, invoices, saved):
        first = invoices[0]
        other = next(i for i in invoices if i.customer_id != first.customer_id)
        guards = PolicyGuards(store, now())
        reservation = guards.reserve_contact(ctx, first.id, "+61 (400) 123-456", "voice", uuid4())
        assert reservation.destination == "+61400123456"
        with pytest.raises(HTTPException, match="recipient_contact_cap"):
            guards.reserve_contact(ctx, other.id, "+61400123456", "email", uuid4())
        guards.release_unused_capacity(ctx, reservation.id)
        replacement = guards.reserve_contact(ctx, other.id, "+61400123456", "voice", uuid4())
        replacement.status = "unknown_outcome"
        store.flush()
        with pytest.raises(HTTPException, match="VERIFIED_NO_EFFECT_REQUIRED"):
            guards.release_unused_capacity(ctx, replacement.id)
        assert replacement.status == "unknown_outcome"
