"""Current-human explicit disposition of genuine worker-produced contact stops."""

from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

from action_support import action_world
from case_promise_support import communication
from evaluations.test_collection_agent import evidence, financial, proposal, run_sdk
from sqlalchemy import func, select

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.cases.service import place_hold, release_hold
from aco.features.identity.models import AuditEvent, Membership
from aco.features.policies.models import CollectionHold, RecipientControl


def test_human_contact_stop_review_exact_versions_retry_and_current_permission() -> None:
    with action_world() as world:
        invoice = world.store.get(InvoiceSnapshot, world.invoice_id)
        assert invoice is not None
        independent = place_hold(
            world.store, world.ctx, invoice.id, "independent_dispute", "Synthetic separate issue"
        )
        world.store.commit()
        message = communication(world.store, world.ctx.tenant_id, [invoice], "Please stop contacting me.")
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
                        "evidence": evidence(message),
                    },
                )
            ],
            extra_reads=[("get_communication_history", invoice.id)],
        )
        before = financial(world)
        path = f"/api/workspaces/{world.ctx.tenant_id}/invoices/{invoice.id}/contact-stops"
        response = world.client.get(path)
        assert response.status_code == 200, response.text
        review = response.json()["items"][0]
        assert review["pending"] and review["can_release"] and review["evidence_complete"]
        assert len(review["original_holds"]) == 4
        release_path = f"/api/workspaces/{world.ctx.tenant_id}/contact-stops/{review['action_id']}/release"
        command = dict(
            request_id=str(uuid4()),
            expected_action_version=review["action_version"],
            reviewed_holds=[
                dict(hold_id=h["id"], expected_version=h["version"]) for h in review["original_holds"]
            ],
            reason="Current human reviewed exact original customer restriction",
        )
        assert world.client.post(release_path, json=dict(command, reason="          ")).status_code == 422
        tampered = dict(command, reviewed_holds=[dict(hold_id=str(uuid4()), expected_version=1)])
        assert world.client.post(release_path, json=tampered).status_code == 409
        assert (
            world.client.post(release_path, json=dict(command, expected_action_version=999)).status_code
            == 409
        )
        member = world.store.scalar(
            select(Membership).where(
                Membership.tenant_id == world.ctx.tenant_id, Membership.user_id == world.ctx.actor_id
            )
        )
        assert member is not None
        member.role, member.version = "viewer", member.version + 1
        world.store.commit()
        assert world.client.get(path).json()["items"][0]["can_release"] is False
        assert world.client.post(release_path, json=command).status_code == 403
        member.role, member.version = "owner", member.version + 1
        world.store.commit()
        # A disposition after capture cannot be silently swapped for fresh hold versions.
        release_hold(
            world.store, world.ctx, UUID(review["original_holds"][0]["id"]), "Earlier explicit disposition"
        )
        world.store.commit()
        assert world.client.post(release_path, json=command).status_code == 409
        fresh = world.client.get(path).json()["items"][0]
        assert fresh["pending"]
        assert sum(h["released_at"] is None for h in fresh["original_holds"]) == 3
        command = dict(
            command,
            request_id=str(uuid4()),
            reviewed_holds=[
                dict(hold_id=h["id"], expected_version=h["version"]) for h in fresh["original_holds"]
            ],
        )
        world.store.rollback()
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: world.client.post(release_path, json=command), range(2)))
        assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
        assert all(not r.json()["pending"] for r in responses)
        assert world.client.post(release_path, json=command).json()["pending"] is False
        assert (
            world.client.post(
                release_path, json=dict(command, reason="Changed exact reviewed reason")
            ).status_code
            == 409
        )
        world.store.expire_all()
        hold = world.store.get(CollectionHold, independent)
        assert hold is not None and hold.released_at is None
        assert world.store.scalar(select(func.count()).select_from(RecipientControl)) == 0
        assert (
            world.store.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "collection.contact_stop_released")
            )
            == 1
        )
        assert financial(world) == before
        assert saved["execution"] == "EXECUTED"
