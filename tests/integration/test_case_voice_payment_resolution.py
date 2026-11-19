"""A spoken claim identifies the issue; independent accounting cash proves payment."""

from datetime import timedelta
from uuid import uuid4

from action_support import World
from evaluations.test_collection_agent import financial
from sqlalchemy import func, select
from voice_support import VoiceCallback, exercise_voice

from aco.features.accounting.models import DemoSourceRecord, InvoiceSnapshot, VerifiedPaymentAllocation
from aco.features.cases.models import CollectionCase
from aco.features.communications.models import Communication
from aco.features.identity.models import now
from aco.features.voice.dto import VoiceSessionView
from aco.features.voice.runtime import VoiceRuntime


async def test_voice_payment_claim_requires_independent_unadjusted_same_invoice_cash() -> None:
    async def scenario(
        world: World, runtime: VoiceRuntime, started: VoiceSessionView, callback: VoiceCallback
    ) -> None:
        db = world.store
        invoice = db.get(InvoiceSnapshot, world.invoice_id)
        assert invoice is not None
        root = f"/api/workspaces/{world.ctx.tenant_id}"
        await callback(
            "payment-claim",
            "upsert_case",
            {
                "invoice_id": str(invoice.id),
                "case_facts": {
                    "invoice_ids": [str(invoice.id)],
                    "issue_type": "payment_not_found",
                    "claim_reference": "customer-payment-reference",
                },
            },
        )
        db.rollback()
        case = db.scalar(select(CollectionCase).where(CollectionCase.issue_type == "payment_not_found"))
        assert case is not None
        path = root + f"/cases/{case.id}"
        detail = world.client.get(path).json()
        voice = detail["evidence"][0]["voice_source"]
        assert detail["evidence"][0]["communication_id"] is None
        origin = {
            "kind": "voice_operational",
            "voice_evidence_id": voice["id"],
            "facts_hash": voice["facts_hash"],
        }
        moved = world.client.patch(
            path,
            json={
                "case_id": str(case.id),
                "expected_version": detail["version"],
                "status": "INVESTIGATING",
                "reason": "Reconcile spoken claim against authoritative cash records",
            },
        )
        assert moved.status_code == 200, moved.text
        current = moved.json()
        command = {
            "case_id": str(case.id),
            "invoice_id": str(invoice.id),
            "expected_version": current["version"],
            "evidence": origin,
            "resolution_kind": "payment_allocation",
            "resolution_action": (
                "Accounting cash independently reconciles the claim; call facts are origin only"
            ),
        }
        # Neither natural-language provenance nor a made-up payment reference proves cash.
        unknown = world.client.post(
            path + "/invoice-resolution", json=command | {"resolution_reference_id": str(uuid4())}
        )
        assert unknown.status_code == 422, unknown.text
        assert unknown.json()["detail"] == "VERIFIED_PAYMENT_EVIDENCE_REQUIRED"
        prefix = "synthetic-voice-cash-" + str(uuid4())
        at = now() - timedelta(seconds=1)
        for suffix, kind, amount, parent in [
            ("good", "cash", 100, None),
            ("bad", "cash", 100, None),
            ("reverse", "reversal", -100, prefix + "bad"),
        ]:
            db.add(
                DemoSourceRecord(
                    tenant_id=invoice.tenant_id,
                    provider_connection_id=invoice.provider_connection_id,
                    organisation_id=invoice.organisation_id,
                    kind="payment",
                    external_id=prefix + suffix + "/1",
                    revision=1,
                    effective_at=at,
                    observed_at=at,
                    payload={
                        "external_payment_id": prefix + suffix,
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
        db.commit()
        imported = world.client.post(
            root + "/accounting/sync", json={"provider_connection_id": str(invoice.provider_connection_id)}
        )
        assert imported.status_code == 200, imported.text
        db.expire_all()
        good = db.scalar(
            select(VerifiedPaymentAllocation).where(
                VerifiedPaymentAllocation.external_payment_id == prefix + "good"
            )
        )
        reversed_cash = db.scalar(
            select(VerifiedPaymentAllocation).where(
                VerifiedPaymentAllocation.external_payment_id == prefix + "bad"
            )
        )
        other = db.scalar(
            select(VerifiedPaymentAllocation).where(
                VerifiedPaymentAllocation.invoice_id != invoice.id, VerifiedPaymentAllocation.kind == "cash"
            )
        )
        assert good is not None and reversed_cash is not None and other is not None
        before = financial(world)
        payments_before = list(
            db.execute(
                select(VerifiedPaymentAllocation.__table__).order_by(VerifiedPaymentAllocation.id)
            ).mappings()
        )
        for reference, expected in [
            (reversed_cash.id, "PAYMENT_ADJUSTMENT_REQUIRES_REVIEW"),
            (other.id, "VERIFIED_PAYMENT_EVIDENCE_REQUIRED"),
        ]:
            rejected = world.client.post(
                path + "/invoice-resolution", json=command | {"resolution_reference_id": str(reference)}
            )
            assert rejected.status_code == 422, rejected.text
            assert rejected.json()["detail"] == expected
        forged = world.client.post(
            path + "/invoice-resolution",
            json=command
            | {"resolution_reference_id": str(good.id), "evidence": origin | {"facts_hash": "0" * 64}},
        )
        assert forged.status_code == 409, forged.text
        assert world.client.get(path).json()["invoice_links"][0]["issue_state"] == "OPEN"
        result = world.client.post(
            path + "/invoice-resolution", json=command | {"resolution_reference_id": str(good.id)}
        )
        assert result.status_code == 200, result.text
        saved = result.json()
        assert saved["invoice_links"][0]["issue_state"] == "RESOLVED"
        assert saved["history"][-1]["details"]["evidence"] == origin
        assert saved["history"][-1]["details"]["resolution_reference_id"] == str(good.id)
        outcomes = world.client.get(
            root + "/impact/case-outcomes",
            params={
                "since": (case.created_at - timedelta(seconds=1)).isoformat(),
                "until": (now() + timedelta(seconds=1)).isoformat(),
            },
        )
        assert outcomes.status_code == 200, outcomes.text
        case_outcome = next(row for row in outcomes.json()["cases"] if row["case_id"] == str(case.id))
        assert "unverifiable_history" not in case_outcome["issues"]
        assert case_outcome["resolution_evidence_ids"] == [
            saved["invoice_links"][0]["resolution_evidence_id"]
        ]
        assert financial(world) == before
        assert (
            list(
                db.execute(
                    select(VerifiedPaymentAllocation.__table__).order_by(VerifiedPaymentAllocation.id)
                ).mappings()
            )
            == payments_before
        )
        assert db.scalar(select(func.count()).select_from(Communication)) == 0

    await exercise_voice("payment-claim-independent-cash", scenario)
