"""Actual current-human reads of retained customer interactions and pending actions."""

from datetime import timedelta
from uuid import uuid4

from accounting_support import seed
from action_support import action_world, enqueue
from identity_support import actor_client
from sqlalchemy import select

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.actions.models import ScheduledAction
from aco.features.communications.models import Communication, CommunicationInvoiceLink, CommunicationThread
from aco.features.identity.models import Membership, ProviderConnection, Tenant, now
from aco.features.policies.models import ContactReservation


def test_customer_activity_counts_original_once_and_exposure_honestly_under_current_viewer() -> None:
    with actor_client("owner") as (client, db, user, tenant):
        seed(client, tenant)
        invoices = list(db.scalars(select(InvoiceSnapshot).where(InvoiceSnapshot.tenant_id == tenant)))
        first = invoices[0]
        second = next(i for i in invoices if i.id != first.id and i.customer_id == first.customer_id)
        unrelated = next(i for i in invoices if i.customer_id != first.customer_id)
        email = ProviderConnection(tenant_id=tenant, provider="demo", provider_type="email")
        db.add(email)
        db.flush()
        at = now()

        def message(invoice: InvoiceSnapshot, direction: str, state: str, age: timedelta) -> Communication:
            thread = CommunicationThread(
                tenant_id=tenant,
                provider_connection_id=email.id,
                origin_invoice_id=invoice.id,
                customer_id=invoice.customer_id,
                purpose="invoice_outbound",
            )
            # One retained thread per invoice; different messages are independent originals.
            existing = db.scalar(
                select(CommunicationThread).where(
                    CommunicationThread.tenant_id == tenant,
                    CommunicationThread.origin_invoice_id == invoice.id,
                    CommunicationThread.provider_connection_id == email.id,
                )
            )
            if existing is not None:
                thread = existing
            else:
                db.add(thread)
                db.flush()
            row = Communication(
                tenant_id=tenant,
                provider_connection_id=email.id,
                thread_id=thread.id,
                origin_invoice_id=invoice.id,
                operation_id=uuid4(),
                direction=direction,
                channel="email",
                content={},
                event_at=at - age,
                delivery_state=state,
                source_state="unavailable",
                synthetic=True,
            )
            db.add(row)
            db.flush()
            return row

        shared = message(first, "incoming", "received", timedelta(hours=2))
        for invoice in (first, second):
            db.add(
                CommunicationInvoiceLink(
                    tenant_id=tenant,
                    communication_id=shared.id,
                    invoice_id=invoice.id,
                    claim_or_context_key=str(invoice.id),
                    provenance="explicit_invoice_reference",
                    confidence="exact",
                    evidence_excerpt="Synthetic scoped reference",
                    evidence_offset=0,
                    classification="explicit_reference",
                )
            )
        message(first, "outgoing", "accepted", timedelta(days=2))
        message(second, "outgoing", "unknown_outcome", timedelta(days=3))
        message(first, "outgoing", "prepared", timedelta(days=4))
        message(first, "incoming", "received", timedelta(days=31))
        message(first, "incoming", "received", timedelta(days=-1))
        message(unrelated, "incoming", "received", timedelta(hours=1))
        for status in ("accepted", "unknown_outcome", "inflight", "reserved", "released"):
            db.add(
                ContactReservation(
                    tenant_id=tenant,
                    invoice_id=first.id,
                    customer_id=first.customer_id,
                    destination="synthetic@example.test",
                    channel="email",
                    operation_id=uuid4(),
                    status=status,
                    reserved_at=at - timedelta(hours=3),
                    submitted_at=None,
                )
            )
        for age in (timedelta(days=8), timedelta(days=-1)):
            db.add(
                ContactReservation(
                    tenant_id=tenant,
                    invoice_id=first.id,
                    customer_id=first.customer_id,
                    destination="synthetic@example.test",
                    channel="email",
                    operation_id=uuid4(),
                    status="unknown_outcome",
                    reserved_at=at - age,
                    submitted_at=None,
                )
            )
        other = Tenant(name="Other read scope", timezone="UTC")
        db.add(other)
        db.flush()
        db.add(Membership(tenant_id=other.id, user_id=user.id, role="owner"))
        db.add(ProviderConnection(tenant_id=other.id, provider="demo", provider_type="accounting"))
        db.commit()
        seed(client, other.id)
        foreign = db.scalar(select(InvoiceSnapshot).where(InvoiceSnapshot.tenant_id == other.id))
        member = db.scalar(
            select(Membership).where(Membership.tenant_id == tenant, Membership.user_id == user.id)
        )
        foreign_member = db.scalar(select(Membership).where(Membership.tenant_id == other.id))
        assert foreign is not None and member is not None and foreign_member is not None
        member.role = "viewer"
        foreign_member.active = False
        db.commit()
        before = [(r.id, r.balance_minor, r.financial_status, r.source_revision) for r in invoices]
        base = f"/api/workspaces/{tenant}"
        response = client.get(base + f"/customers/{first.customer_id}")
        assert response.status_code == 200
        result = response.json()
        assert isinstance(result["interactions"], dict), result["interactions"]
        activity = result["interactions"]
        assert activity["original_count"] == 4
        assert activity["incoming_count"] == 1 and activity["outgoing_count"] == 3
        assert activity["window_days"] == 30
        assert activity["as_of_at"] and activity["window_start"]
        assert {r["state"]: r["count"] for r in activity["groups"]} == {
            "received": 1,
            "accepted": 1,
            "unknown_outcome": 1,
            "prepared": 1,
        }
        exposure = result["contact_frequency"]
        assert exposure["last_24_hours"] == 4 and exposure["last_7_days"] == 4
        assert exposure["states"] == {
            "reserved": 1,
            "inflight": 1,
            "accepted": 1,
            "connected": 0,
            "unknown_outcome": 1,
        }
        assert "synthetic@example.test" not in response.text
        empty_customer = next(
            i.customer_id for i in invoices if i.customer_id not in {first.customer_id, unrelated.customer_id}
        )
        empty = client.get(base + f"/customers/{empty_customer}").json()
        assert empty["interactions"]["original_count"] == 0
        assert empty["contact_frequency"]["last_7_days"] == 0
        for role in ("owner", "finance_manager", "operator", "viewer"):
            member.role = role
            db.commit()
            permitted = client.get(base + f"/customers/{first.customer_id}")
            assert permitted.status_code == 200
            assert permitted.json()["interactions"]["original_count"] == 4
        assert client.get(base + f"/customers/{foreign.customer_id}").status_code == 404
        assert client.get(f"/api/workspaces/{other.id}/customers/{foreign.customer_id}").status_code == 403
        member.active = False
        db.commit()
        assert client.get(base + f"/customers/{first.customer_id}").status_code == 403
        db.expire_all()
        assert [(r.id, r.balance_minor, r.financial_status, r.source_revision) for r in invoices] == before


def test_invoice_next_saved_pending_action_is_actual_scoped_and_not_eligibility() -> None:
    with action_world() as w:
        later = enqueue(w, due_at=now() + timedelta(days=2))
        earlier = enqueue(
            w,
            kind="internal_task",
            due_at=now() + timedelta(days=1),
            payload={
                "title": "review",
                "source_action_id": str(later.action_id),
                "source_event_id": str(w.source_event_id),
                "invoice_id": str(w.invoice_id),
            },
        )
        member = w.store.scalar(select(Membership).where(Membership.tenant_id == w.ctx.tenant_id))
        assert member is not None
        member.role = "viewer"
        w.store.commit()
        base = f"/api/workspaces/{w.ctx.tenant_id}"
        detail = w.client.get(base + f"/receivables/{w.invoice_id}").json()
        next_action = detail["invoice"]["next_action"]
        assert isinstance(next_action, dict), next_action
        assert next_action["availability"] == "saved_pending"
        assert next_action["id"] == str(earlier.action_id)
        assert next_action["kind"] == "internal_task" and next_action["status"] == "queued"
        assert next_action["channel"] == "internal"
        assert next_action["eligibility"] == "not_evaluated"
        assert detail["sections"] == {
            "communications": "available",
            "cases": "available",
            "promises": "available",
            "tasks": "available",
            "audit": "unavailable",
        }
        listed = w.client.get(base + "/receivables?page_size=100").json()
        assert next(x for x in listed["items"] if x["id"] == str(w.invoice_id))["next_action"] == next_action
        assert all(
            x["next_action"]["availability"] == "none_scheduled"
            for x in listed["items"]
            if x["id"] != str(w.invoice_id)
        )
        uncertain = w.store.get(ScheduledAction, earlier.action_id)
        assert uncertain is not None
        uncertain.status = "unknown_outcome"
        w.store.commit()
        after_unknown = w.client.get(base + f"/receivables/{w.invoice_id}").json()["invoice"]["next_action"]
        assert after_unknown["id"] == str(later.action_id)
        for key in (earlier.action_id, later.action_id):
            row = w.store.get(ScheduledAction, key)
            assert row is not None
            row.status = "cancelled"
        w.store.commit()
        empty = w.client.get(base + f"/receivables/{w.invoice_id}").json()["invoice"]["next_action"]
        assert empty["availability"] == "none_scheduled" and empty["id"] is None
