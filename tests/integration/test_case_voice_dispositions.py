"""Signed operational issue origin supports human disposition, never financial proof."""

from uuid import uuid4

from action_support import World
from sqlalchemy import func, select
from voice_support import VoiceCallback, exercise_voice

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.cases.models import CollectionCase
from aco.features.communications.models import Communication
from aco.features.policies.contact_stop import pending_contact_stops
from aco.features.voice.dto import VoiceSessionView
from aco.features.voice.runtime import VoiceRuntime


async def test_signed_voice_origin_disposition_and_admin_closure_require_exact_own_source() -> None:
    async def scenario(
        world: World, runtime: VoiceRuntime, started: VoiceSessionView, callback: VoiceCallback
    ) -> None:
        db = world.store
        invoice = db.get(InvoiceSnapshot, world.invoice_id)
        assert invoice is not None
        before = (invoice.balance_minor, invoice.financial_status, invoice.source_revision)
        root = f"/api/workspaces/{world.ctx.tenant_id}"
        db.rollback()
        for issue in ["stop_contact", "wrong_amount"]:
            await callback(
                "case-" + issue,
                "upsert_case",
                {
                    "invoice_id": str(invoice.id),
                    "case_facts": {
                        "invoice_ids": [str(invoice.id)],
                        "issue_type": issue,
                        "claim_reference": "specialist-" + issue,
                    },
                },
            )
        # Current restriction is a separate genuine signed action, not inferred from issue text.
        await callback(
            "actual-stop",
            "request_collection_hold",
            {
                "invoice_id": str(invoice.id),
                "hold_facts": {"invoice_ids": [str(invoice.id)], "reason_code": "customer_contact_stop"},
            },
        )
        db.rollback()
        cases = {row.issue_type: row.id for row in db.scalars(select(CollectionCase))}

        def reference(kind: str) -> dict[str, object]:
            detail = world.client.get(root + f"/cases/{cases[kind]}").json()
            source = detail["evidence"][0]["voice_source"]
            assert detail["evidence"][0]["communication_id"] is None
            return {
                "kind": "voice_operational",
                "voice_evidence_id": source["id"],
                "facts_hash": source["facts_hash"],
            }

        stop_origin, amount_origin = reference("stop_contact"), reference("wrong_amount")
        stop_path = root + f"/cases/{cases['stop_contact']}"
        current = world.client.get(stop_path).json()
        attach = {"case_id": current["id"], "expected_version": current["version"], "evidence": stop_origin}
        assert (
            world.client.post(stop_path + "/evidence", json=attach | {"evidence": amount_origin}).status_code
            == 403
        )
        assert world.client.post(stop_path + "/evidence", json=attach).status_code == 200
        assert world.client.get(stop_path).json()["version"] == current["version"]
        moved = world.client.patch(
            stop_path,
            json={
                "case_id": current["id"],
                "expected_version": current["version"],
                "status": "INVESTIGATING",
                "reason": "Human reviews retained origin and enforced restriction",
            },
        )
        assert moved.status_code == 200, moved.text
        current = moved.json()
        refs = world.client.get(
            stop_path + "/disposition-references", params={"invoice_id": str(invoice.id)}
        ).json()["items"]
        assert refs
        target = refs[0]
        command = {
            "case_id": current["id"],
            "invoice_id": str(invoice.id),
            "expected_version": current["version"],
            "evidence": stop_origin,
            "resolution_kind": "verified_contact_restriction",
            "resolution_reference_id": target["id"],
            "expected_reference_version": target["version"],
            "expected_reference_hash": target["material_hash"],
            "resolution_action": (
                "Actual enforced customer restriction verified; retained call facts only identify the issue"
            ),
        }
        for evidence, code in [
            (stop_origin | {"facts_hash": "0" * 64}, 409),
            (amount_origin, 403),
            (stop_origin | {"voice_evidence_id": str(uuid4())}, 403),
        ]:
            refused = world.client.post(
                stop_path + "/invoice-resolution", json=command | {"evidence": evidence}
            )
            assert refused.status_code == code, refused.text
        wrong_ref = world.client.post(
            stop_path + "/invoice-resolution", json=command | {"resolution_reference_id": str(uuid4())}
        )
        assert wrong_ref.status_code == 422, wrong_ref.text
        saved = world.client.post(stop_path + "/invoice-resolution", json=command)
        assert saved.status_code == 200, saved.text
        assert saved.json()["invoice_links"][0]["issue_state"] == "RESOLVED"
        history = saved.json()["history"][-1]["details"]
        assert history["evidence"] == stop_origin
        assert "communication_id" not in history["evidence"]
        assert pending_contact_stops(db, invoice)
        amount_path = root + f"/cases/{cases['wrong_amount']}"
        current = world.client.get(amount_path).json()
        moved = world.client.patch(
            amount_path,
            json={
                "case_id": current["id"],
                "expected_version": current["version"],
                "status": "INVESTIGATING",
                "reason": "Review unverified amount claim",
            },
        )
        assert moved.status_code == 200
        current = moved.json()
        refused = world.client.post(
            amount_path + "/invoice-resolution",
            json={
                "case_id": current["id"],
                "invoice_id": str(invoice.id),
                "expected_version": current["version"],
                "evidence": amount_origin,
                "resolution_kind": "documentary_confirmation",
                "resolution_action": "Unverified voice claim is not proof of amount correction",
            },
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"] == "DOCUMENTARY_DISPOSITION_EVIDENCE_REQUIRED"
        close = {
            "case_id": current["id"],
            "expected_version": current["version"],
            "status": "CLOSED",
            "closure_reason": "withdrawn",
            "reason": (
                "Current human administratively withdraws this unverified claim; no successful resolution"
            ),
            "evidence": amount_origin,
        }
        denied = world.client.patch(amount_path, json=close | {"evidence": stop_origin})
        assert denied.status_code == 403, denied.text
        wrong_hash = world.client.patch(
            amount_path, json=close | {"evidence": amount_origin | {"facts_hash": "0" * 64}}
        )
        assert wrong_hash.status_code == 409, wrong_hash.text
        assert (
            world.client.patch(amount_path, json=close, headers={"x-csrf-token": "invalid"}).status_code
            == 403
        )
        closed = world.client.patch(amount_path, json=close)
        assert closed.status_code == 200, closed.text
        assert closed.json()["closure_reason"] == "withdrawn"
        assert closed.json()["invoice_links"][0]["issue_state"] == "OPEN"
        db.rollback()
        assert db.scalar(select(func.count()).select_from(Communication)) == 0
        db.refresh(invoice)
        assert (invoice.balance_minor, invoice.financial_status, invoice.source_revision) == before

    await exercise_voice("case-human-specialist", scenario)
