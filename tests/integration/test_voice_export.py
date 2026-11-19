"""Actual signed operational voice facts retain safe export source links, never call content."""

from datetime import timedelta

from action_support import World
from sqlalchemy import select
from voice_support import VoiceCallback, exercise_voice

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.identity.models import Membership, Tenant, now
from aco.features.voice.dto import VoiceSessionView
from aco.features.voice.models import VoiceBusinessEvidence
from aco.features.voice.runtime import VoiceRuntime


async def test_signed_voice_business_sources_export_with_exact_tenant_and_no_private_content() -> None:
    async def scenario(
        world: World, runtime: VoiceRuntime, started: VoiceSessionView, callback: VoiceCallback
    ) -> None:
        db = world.store
        invoice = db.get(InvoiceSnapshot, world.invoice_id)
        assert invoice is not None
        before = (invoice.balance_minor, invoice.source_revision, invoice.financial_status)
        invoice_id = str(invoice.id)
        db.rollback()
        await callback(
            "export-case",
            "upsert_case",
            {
                "invoice_id": invoice_id,
                "case_facts": {
                    "invoice_ids": [invoice_id],
                    "issue_type": "wrong_amount",
                    "claim_reference": "private-normalized-reference",
                },
            },
        )
        await callback(
            "export-promise",
            "record_promise",
            {
                "invoice_id": invoice_id,
                "promise_facts": {
                    "invoice_ids": [invoice_id],
                    "amount": {"minor_units": 4000, "currency": "AUD", "exponent": 2},
                    "due_date": "2026-12-01",
                    "timezone": "UTC",
                },
            },
        )
        db.rollback()
        sources = list(db.scalars(select(VoiceBusinessEvidence).order_by(VoiceBusinessEvidence.fact_kind)))
        assert len(sources) == 2
        expected = {s.fact_kind: (str(s.id), s.facts_hash) for s in sources}
        other = Tenant(name="Other export voice company", timezone="UTC")
        db.add(other)
        db.flush()
        db.add(Membership(tenant_id=other.id, user_id=world.ctx.actor_id, role="owner"))
        db.commit()
        other_id = other.id
        body = {
            "resources": ["cases", "promises", "voice_sessions", "usage"],
            "created_from": (now() - timedelta(days=1)).isoformat(),
            "created_before": (now() + timedelta(minutes=1)).isoformat(),
        }
        db.rollback()
        response = world.client.post(f"/api/workspaces/{world.ctx.tenant_id}/exports", json=body)
        assert response.status_code == 200, response.text
        records = response.json()["records"]
        commitment = records["promises"][0]
        assert commitment["source_communication_id"] is None
        assert commitment["source_voice_evidence_id"] == expected["promise"][0]
        assert commitment["status"] == "NEEDS_CONFIRMATION"
        assert commitment["amount"]["minor_units"] == "4000"
        evidence = records["cases"][0]["evidence"][0]
        assert evidence["communication_id"] is None
        assert evidence["voice_evidence_id"] == expected["case"][0]
        session = records["voice_sessions"][0]
        assert session["id"] == str(started.id)
        retained = session["business_evidence"]
        assert {r["fact_kind"]: (r["id"], r["facts_hash"]) for r in retained} == expected
        assert all(
            r["source_state"] == "VOICE_OPERATIONAL_METADATA"
            and r["synthetic"]
            and r["evidence_mode"] == "MOCKED_HTTP"
            for r in retained
        )
        assert all(r["provider_connection_id"] == session["connection_id"] for r in retained)
        usage = [r for r in records["usage"] if r["provider_connection_id"] == session["connection_id"]]
        assert usage and all(
            r["cost_amount"] is None and r["cost_quality"] == "UNKNOWN"
            for event in usage
            for r in event["cost_history"]
        )
        for excluded in (
            "private-normalized-reference",
            "facts_json",
            '"facts":',
            "excerpt",
            "transcript",
            "acoCallCapability",
            "synthetic-signing-secret",
            "object_key",
        ):
            assert excluded not in str(records)
        foreign = world.client.post(f"/api/workspaces/{other_id}/exports", json=body)
        assert foreign.status_code == 200, foreign.text
        assert all(not rows for rows in foreign.json()["records"].values())
        assert all(source_id not in foreign.text for source_id, _ in expected.values())
        db.rollback()
        db.refresh(invoice)
        assert (invoice.balance_minor, invoice.source_revision, invoice.financial_status) == before

    await exercise_voice("export-operational-facts", scenario)
