"""Exact source-backed specialist dispositions never alter accounting or resume contact."""

from datetime import timedelta
from uuid import UUID, uuid4

from action_support import World, action_world
from case_promise_support import communication
from evaluations.test_collection_agent import evidence, financial, proposal, run_sdk
from fastapi import FastAPI
from identity_support import add_actor
from sqlalchemy import select
from starlette.testclient import TestClient

from aco.features.accounting.models import (
    CustomerContact,
    DemoSourceRecord,
    InvoiceSnapshot,
    VerifiedPaymentAllocation,
)
from aco.features.cases.dto import CaseView, TransitionCase, UpsertCase
from aco.features.cases.service import CaseService
from aco.features.communications.models import Communication
from aco.features.identity.models import Membership, now
from aco.features.policies.contact_stop import pending_contact_stops
from aco.features.policies.models import CollectionHold, RecipientControl


def investigate(world: World, invoice: InvoiceSnapshot, source: Communication, issue: str) -> CaseView:
    service = CaseService(world.store)
    case = service.upsert_case(
        world.ctx,
        UpsertCase.model_validate(
            {
                "customer_id": invoice.customer_id,
                "invoice_ids": [invoice.id],
                "issue_type": issue,
                "claim_key": "synthetic-specialist-" + issue + "-" + str(invoice.id),
                "substance": "Synthetic source-backed issue requiring explicit human disposition",
                "evidence": evidence(source),
            }
        ),
    ).case
    case = service.transition_case(
        world.ctx,
        TransitionCase(
            case_id=case.id,
            expected_version=case.version,
            status="INVESTIGATING",
            reason="Reviewing actual source evidence and retained restrictions",
        ),
    )
    world.store.commit()
    return case


def resolve(
    world: World,
    case: CaseView,
    invoice: InvoiceSnapshot,
    source: Communication,
    kind: str,
    reference: UUID,
    client: TestClient | None = None,
) -> CaseView:
    client = client or world.client
    options = client.get(
        f"/api/workspaces/{world.ctx.tenant_id}/cases/{case.id}/disposition-references",
        params={"invoice_id": str(invoice.id)},
    )
    assert options.status_code == 200, options.text
    captured = next(v for v in options.json()["items"] if v["id"] == str(reference))
    path = f"/api/workspaces/{world.ctx.tenant_id}/cases/{case.id}/invoice-resolution"
    command = {
        "case_id": str(case.id),
        "invoice_id": str(invoice.id),
        "expected_version": case.version,
        "evidence": evidence(source),
        "resolution_kind": kind,
        "resolution_reference_id": str(reference),
        "expected_reference_version": captured["version"],
        "expected_reference_hash": captured["material_hash"],
        "resolution_action": "Current human reviewed the exact retained source disposition",
    }
    for change, status in [
        ({"expected_version": case.version + 1}, 409),
        ({"expected_reference_version": captured["version"] + 1}, 409),
        ({"expected_reference_hash": "0" * 64}, 409),
        ({"resolution_reference_id": str(uuid4())}, 422),
        ({"resolution_kind": "documentary_confirmation"}, 422),
        ({"invoice_id": str(uuid4())}, 403),
        ({"expected_reference_hash": None}, 422),
    ]:
        rejected = client.post(path, json={**command, **change})
        assert rejected.status_code == status, rejected.text
    assert client.post(path, json=command, headers={"x-csrf-token": "invalid"}).status_code == 403
    unchanged = client.get(f"/api/workspaces/{world.ctx.tenant_id}/cases/{case.id}")
    assert unchanged.json()["version"] == case.version
    assert unchanged.json()["invoice_links"][0]["issue_state"] == "OPEN"
    if client is not world.client:
        # Existing independent review never authorizes its original reviewer to
        # silently reuse the second human's captured reference.
        assert world.client.post(path, json=command).status_code == 422
    result = client.post(path, json=command)
    assert result.status_code == 200, result.text
    return CaseView.model_validate_json(result.text)


def test_refund_disposition_uses_actual_imported_refund_without_financial_write() -> None:
    with action_world() as world:
        invoice = world.store.get(InvoiceSnapshot, world.invoice_id)
        assert invoice is not None
        source = communication(
            world.store,
            world.ctx.tenant_id,
            [invoice],
            "Synthetic fixture: please review the refund of AUD 1.00.",
        )
        case = investigate(world, invoice, source, "refund")
        at = now() - timedelta(seconds=1)
        key = "synthetic-refund-" + str(uuid4())
        for suffix, kind, amount, parent in [
            ("cash", "cash", 100, None),
            ("refund", "refund", -100, key + "cash"),
        ]:
            world.store.add(
                DemoSourceRecord(
                    tenant_id=invoice.tenant_id,
                    provider_connection_id=invoice.provider_connection_id,
                    organisation_id=invoice.organisation_id,
                    kind="payment",
                    external_id=key + suffix + "/1",
                    revision=1,
                    effective_at=at,
                    observed_at=at,
                    payload={
                        "external_payment_id": key + suffix,
                        "external_allocation_id": "1",
                        "external_invoice_id": invoice.external_invoice_id,
                        "kind": kind,
                        "amount": {
                            "minor_units": amount,
                            "currency": invoice.currency,
                            "exponent": invoice.exponent,
                        },
                        "parent_payment_id": parent,
                        "parent_allocation_id": "1" if parent else None,
                    },
                )
            )
        world.store.commit()
        result = world.client.post(
            f"/api/workspaces/{world.ctx.tenant_id}/accounting/sync",
            json={"provider_connection_id": str(invoice.provider_connection_id)},
        )
        assert result.status_code == 200, result.text
        world.store.expire_all()
        refund = world.store.scalar(
            select(VerifiedPaymentAllocation).where(
                VerifiedPaymentAllocation.invoice_id == invoice.id, VerifiedPaymentAllocation.kind == "refund"
            )
        )
        assert refund is not None
        # A genuine refund on another invoice cannot become this issue's reference.
        other = world.store.scalar(
            select(InvoiceSnapshot).where(
                InvoiceSnapshot.customer_id == invoice.customer_id, InvoiceSnapshot.id != invoice.id
            )
        )
        assert other is not None
        other_source = communication(
            world.store,
            world.ctx.tenant_id,
            [other],
            "Synthetic independent refund question for this other invoice.",
        )
        other_case = investigate(world, other, other_source, "refund")
        refs = world.client.get(
            f"/api/workspaces/{world.ctx.tenant_id}/cases/{case.id}/disposition-references",
            params={"invoice_id": str(invoice.id)},
        ).json()["items"]
        captured = next(item for item in refs if item["id"] == str(refund.id))
        foreign = world.client.post(
            f"/api/workspaces/{world.ctx.tenant_id}/cases/{other_case.id}/invoice-resolution",
            json={
                "case_id": str(other_case.id),
                "invoice_id": str(other.id),
                "expected_version": other_case.version,
                "evidence": evidence(other_source),
                "resolution_kind": "source_refund",
                "resolution_reference_id": str(refund.id),
                "expected_reference_version": captured["version"],
                "expected_reference_hash": captured["material_hash"],
                "resolution_action": "Incorrectly reuse another invoice refund",
            },
        )
        assert foreign.status_code == 422, foreign.text
        before = financial(world)
        result = resolve(world, case, invoice, source, "source_refund", refund.id)
        assert result.invoice_links[0].issue_state == "RESOLVED"
        assert financial(world) == before


def test_stop_contact_disposition_retains_genuine_enforced_restriction() -> None:
    with action_world() as world:
        invoice = world.store.get(InvoiceSnapshot, world.invoice_id)
        assert invoice is not None
        source = communication(world.store, world.ctx.tenant_id, [invoice], "Please stop contacting me.")
        saved = run_sdk(
            world,
            [invoice],
            [
                proposal(
                    invoice,
                    "request_collection_hold",
                    hold_command={
                        "invoice_id": str(invoice.id),
                        "customer_id": str(invoice.customer_id),
                        "evidence": evidence(source),
                    },
                )
            ],
            extra_reads=[("get_communication_history", invoice.id)],
        )
        assert saved["status"] == "completed"
        action_id = UUID(saved["result"]["proposals"][0]["action_id"])
        case = investigate(world, invoice, source, "stop_contact")
        before = financial(world)
        result = resolve(world, case, invoice, source, "verified_contact_restriction", action_id)
        assert result.invoice_links[0].issue_state == "RESOLVED"
        assert pending_contact_stops(world.store, invoice)
        holds = list(
            world.store.scalars(
                select(CollectionHold).where(CollectionHold.reason == "contact_stop_pending_review")
            )
        )
        assert holds and all(h.released_at is None for h in holds)
        assert financial(world) == before
        # A later explicit current-human release removes the reference; stale
        # captured restriction evidence must not resolve an issue again.
        refs_path = f"/api/workspaces/{world.ctx.tenant_id}/cases/{case.id}/disposition-references"
        captured = world.client.get(refs_path, params={"invoice_id": str(invoice.id)}).json()["items"][0]
        review = world.client.get(
            f"/api/workspaces/{world.ctx.tenant_id}/invoices/{invoice.id}/contact-stops"
        ).json()["items"][0]
        world.store.rollback()
        released = world.client.post(
            f"/api/workspaces/{world.ctx.tenant_id}/contact-stops/{action_id}/release",
            json={
                "request_id": str(uuid4()),
                "expected_action_version": review["action_version"],
                "reviewed_holds": [
                    {"hold_id": h["id"], "expected_version": h["version"]} for h in review["original_holds"]
                ],
                "reason": "Independent explicit synthetic restriction review; no contact authority granted",
            },
        )
        assert released.status_code == 200, released.text
        assert world.client.get(refs_path, params={"invoice_id": str(invoice.id)}).json()["items"] == []
        rejected = world.client.post(
            f"/api/workspaces/{world.ctx.tenant_id}/cases/{case.id}/invoice-resolution",
            json={
                "case_id": str(case.id),
                "invoice_id": str(invoice.id),
                "expected_version": result.version,
                "evidence": evidence(source),
                "resolution_kind": "verified_contact_restriction",
                "resolution_reference_id": str(action_id),
                "expected_reference_version": captured["version"],
                "expected_reference_hash": captured["material_hash"],
                "resolution_action": "Attempt to reuse a released restriction",
            },
        )
        assert rejected.status_code == 422, rejected.text
        assert financial(world) == before


def test_contact_correction_requires_current_independent_owner_review_and_preserves_optout() -> None:
    with action_world() as world:
        invoice = world.store.get(InvoiceSnapshot, world.invoice_id)
        assert invoice is not None
        contact = world.store.scalar(
            select(CustomerContact).where(CustomerContact.customer_id == invoice.customer_id)
        )
        assert contact is not None
        source = communication(
            world.store,
            world.ctx.tenant_id,
            [invoice],
            "Synthetic fixture: please verify the current contact destination.",
        )
        case = investigate(world, invoice, source, "contact_correction")
        reviewed = world.client.put(
            f"/api/workspaces/{world.ctx.tenant_id}/policy-recipients/{contact.id}",
            json={
                "expected_version": 0,
                "expected_contact_revision": contact.source_revision,
                "expected_destination": contact.email,
                "destination_verified": True,
                "opt_out": True,
                "allowed_channels": [],
                "evidence": "Synthetic independent Owner verified current source destination authority",
            },
        )
        assert reviewed.status_code == 200, reviewed.text
        app = world.client.app
        assert isinstance(app, FastAPI)
        user, finance = add_actor(world.store, app, "case-finance@synthetic.invalid")
        world.store.add(Membership(tenant_id=world.ctx.tenant_id, user_id=user.id, role="finance_manager"))
        world.store.commit()
        control = world.store.scalar(
            select(RecipientControl).where(RecipientControl.contact_id == contact.id)
        )
        assert control is not None
        before = financial(world)
        refs_path = f"/api/workspaces/{world.ctx.tenant_id}/cases/{case.id}/disposition-references"
        old = finance.get(refs_path, params={"invoice_id": str(invoice.id)}).json()["items"][0]
        update = world.client.put(
            f"/api/workspaces/{world.ctx.tenant_id}/policy-recipients/{contact.id}",
            json={
                "expected_version": control.version,
                "expected_contact_revision": contact.source_revision,
                "expected_destination": contact.email,
                "destination_verified": True,
                "opt_out": True,
                "allowed_channels": [],
                "evidence": "Synthetic revised independent verification evidence",
            },
        )
        assert update.status_code == 200, update.text
        stale = finance.post(
            f"/api/workspaces/{world.ctx.tenant_id}/cases/{case.id}/invoice-resolution",
            json={
                "case_id": str(case.id),
                "invoice_id": str(invoice.id),
                "expected_version": case.version,
                "evidence": evidence(source),
                "resolution_kind": "verified_contact_correction",
                "resolution_reference_id": str(control.id),
                "expected_reference_version": old["version"],
                "expected_reference_hash": old["material_hash"],
                "resolution_action": "Reviewed old contact evidence",
            },
        )
        assert stale.status_code == 409, stale.text
        world.store.rollback()
        member = world.store.scalar(
            select(Membership).where(
                Membership.tenant_id == world.ctx.tenant_id, Membership.user_id == world.ctx.actor_id
            )
        )
        assert member is not None
        member.active = False
        member.version += 1
        world.store.commit()
        assert finance.get(refs_path, params={"invoice_id": str(invoice.id)}).json()["items"] == []
        member.active = True
        member.version += 1
        world.store.commit()
        result = resolve(world, case, invoice, source, "verified_contact_correction", control.id, finance)
        assert result.invoice_links[0].issue_state == "RESOLVED"
        world.store.refresh(control)
        assert control.opt_out is True and control.allowed_channels == []
        resolution = next(h for h in result.history if h.event == "case.invoice_issue_resolved")
        snapshot = resolution.details["disposition_reference"]
        assert isinstance(snapshot, dict)
        assert snapshot["version"] == control.version and snapshot["id"] == str(control.id)
        assert snapshot["details"]["destination"] == contact.email
        assert financial(world) == before
