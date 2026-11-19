"""Signed typed call facts are operational evidence, never a fabricated email/transcript."""

import json
from uuid import uuid4

import pytest
from action_support import World
from fastapi import HTTPException
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError
from voice_support import VoiceCallback, exercise_voice

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.artifacts.models import Artifact
from aco.features.cases.models import CaseEvidence, CollectionCase
from aco.features.communications.models import Communication
from aco.features.promises.models import PaymentPromise
from aco.features.voice.dto import VoiceSessionView
from aco.features.voice.models import VoiceBusinessEvidence, VoiceSession
from aco.features.voice.runtime import VoiceRuntime
from aco.integrations.ports.voice import VoiceEvent


@pytest.mark.parametrize(
    "boundary", ["success", "verbatim", "forged_customer", "unselected", "unsigned_direct"]
)
async def test_signed_operational_case_promise_and_explicit_human_source_review(boundary: str) -> None:
    async def scenario(
        world: World, runtime: VoiceRuntime, started: VoiceSessionView, callback: VoiceCallback
    ) -> None:
        db = world.store
        invoice = db.get(InvoiceSnapshot, world.invoice_id)
        assert invoice
        before = (invoice.balance_minor, invoice.financial_status, invoice.source_revision)
        facts: dict[str, object] = dict(
            invoice_ids=[str(invoice.id)], issue_type="wrong_amount", claim_reference="contract-rate-2026"
        )
        if boundary == "verbatim":
            facts["raw_text"] = "Unapproved verbatim speech must never persist"
        elif boundary == "forged_customer":
            facts["customer_id"] = str(uuid4())
        elif boundary == "unselected":
            other = db.scalar(
                select(InvoiceSnapshot).where(
                    InvoiceSnapshot.customer_id == invoice.customer_id, InvoiceSnapshot.id != invoice.id
                )
            )
            assert other
            facts["invoice_ids"] = [str(other.id)]
        command: dict[str, object] = dict(invoice_id=str(invoice.id), case_facts=facts)
        db.rollback()
        if boundary == "unsigned_direct":
            session = db.get(VoiceSession, started.id)
            assert session and session.provider_call_id
            with pytest.raises(HTTPException) as denied:
                runtime.tool(
                    db,
                    session,
                    VoiceEvent(
                        provider_call_id=session.provider_call_id,
                        event_key="tool:unsigned-direct",
                        kind="tool.requested",
                        invocation_id="unsigned-direct",
                        tool="upsert_case",
                        arguments=command,
                    ),
                )
            assert denied.value.detail == "SIGNED_VOICE_EVENT_REQUIRED"
            db.rollback()
            assert db.scalar(select(func.count()).select_from(VoiceBusinessEvidence)) == 0
            assert db.scalar(select(func.count()).select_from(CollectionCase)) == 0
            return
        if boundary != "success":
            with pytest.raises(HTTPException):
                await callback("operational-case", "upsert_case", command)
            db.rollback()
            assert db.scalar(select(func.count()).select_from(CollectionCase)) == 0
            assert db.scalar(select(func.count()).select_from(Communication)) == 0
            return
        saved_case = await callback("operational-case", "upsert_case", command)
        assert await callback("operational-case", "upsert_case", command) == saved_case
        promise: dict[str, object] = dict(
            invoice_id=str(invoice.id),
            promise_facts=dict(
                invoice_ids=[str(invoice.id)],
                amount=dict(minor_units=4000, currency="AUD", exponent=2),
                due_date="2026-12-01",
                timezone="UTC",
            ),
        )
        saved_promise = await callback("operational-promise", "record_promise", promise)
        assert await callback("operational-promise", "record_promise", promise) == saved_promise
        db.rollback()
        case = db.scalar(select(CollectionCase))
        evidence = db.scalar(select(CaseEvidence))
        commitment = db.scalar(select(PaymentPromise))
        assert case and evidence and commitment
        assert evidence.communication_id is None and evidence.voice_evidence_id
        assert commitment.source_communication_id is None and commitment.source_voice_evidence_id
        assert commitment.status == "NEEDS_CONFIRMATION"
        voice_record = db.get(VoiceBusinessEvidence, commitment.source_voice_evidence_id)
        assert voice_record
        original_hash = voice_record.facts_hash
        for mutation in (
            update(VoiceBusinessEvidence)
            .where(VoiceBusinessEvidence.id == voice_record.id)
            .values(facts_hash="0" * 64),
            delete(VoiceBusinessEvidence).where(VoiceBusinessEvidence.id == voice_record.id),
        ):
            with pytest.raises(DBAPIError), db.begin_nested():
                db.execute(mutation)
        db.refresh(voice_record)
        assert voice_record.facts_hash == original_hash

        assert db.scalar(select(func.count()).select_from(Communication)) == 0
        assert db.scalar(select(func.count()).select_from(Artifact)) == 0
        root = f"/api/workspaces/{world.ctx.tenant_id}"
        detail = world.client.get(root + f"/promises/{commitment.id}")
        assert detail.status_code == 200
        voice_source = detail.json()["voice_source"]
        assert voice_source["session_id"] == str(started.id)
        assert voice_source["provider_call_id"] == started.provider_call_id
        assert voice_source["source_state"] == "VOICE_OPERATIONAL_METADATA"
        assert json.loads(voice_source["facts_json"])["amount"]["minor_units"] == 4000
        reference = dict(
            kind="voice_operational",
            voice_evidence_id=voice_source["id"],
            facts_hash=voice_source["facts_hash"],
        )
        forbidden = world.client.post(
            root + "/promises",
            json=dict(
                customer_id=str(invoice.customer_id),
                invoice_ids=[str(invoice.id)],
                evidence=reference,
                amount=dict(minor_units=4000, currency="AUD", exponent=2),
                due_date="2026-12-01",
                timezone="UTC",
            ),
        )
        assert forbidden.status_code == 403, forbidden.text
        review = dict(
            promise_id=str(commitment.id),
            expected_version=detail.json()["version"],
            confirmation_evidence=reference,
            reason="Human reviewed these exact signed operational facts, without verifying payment",
        )
        stale = world.client.post(
            root + f"/promises/{commitment.id}/confirm",
            json=review | {"confirmation_evidence": reference | {"facts_hash": "0" * 64}},
        )
        assert stale.status_code == 409, stale.text
        confirmed = world.client.post(root + f"/promises/{commitment.id}/confirm", json=review)
        assert confirmed.status_code == 200 and confirmed.json()["status"] == "ACTIVE", confirmed.text
        cancellation = dict(
            promise_id=str(commitment.id),
            expected_version=confirmed.json()["version"],
            evidence=reference,
            reason="Human cancels this captured operational commitment after review",
        )
        denied_cancel = world.client.post(
            root + f"/promises/{commitment.id}/cancel",
            json=cancellation | {"evidence": reference | {"facts_hash": "0" * 64}},
        )
        assert denied_cancel.status_code == 409, denied_cancel.text
        cancelled = world.client.post(root + f"/promises/{commitment.id}/cancel", json=cancellation)
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED", cancelled.text
        db.rollback()
        assert db.scalar(select(func.count()).select_from(Communication)) == 0
        db.refresh(invoice)
        assert (invoice.balance_minor, invoice.financial_status, invoice.source_revision) == before

    detail = await exercise_voice("operational-facts", scenario)
    assert detail and not detail.transcript_available and not detail.recording_available


@pytest.mark.parametrize("release_while_active", [False, True])
async def test_signed_contact_stop_is_one_way_replayed_and_explicitly_released(
    release_while_active: bool,
) -> None:
    import hashlib
    import hmac

    from aco.features.identity.models import now
    from aco.features.policies.contact_stop import pending_contact_stops
    from aco.features.policies.models import CollectionHold
    from aco.features.voice.models import VoiceSession

    async def scenario(
        world: World, runtime: VoiceRuntime, started: VoiceSessionView, callback: VoiceCallback
    ) -> None:
        db = world.store
        invoice = db.get(InvoiceSnapshot, world.invoice_id)
        assert invoice
        customer_invoices = list(
            db.scalars(
                select(InvoiceSnapshot).where(
                    InvoiceSnapshot.tenant_id == invoice.tenant_id,
                    InvoiceSnapshot.provider_connection_id == invoice.provider_connection_id,
                    InvoiceSnapshot.customer_id == invoice.customer_id,
                )
            )
        )
        financial = {
            i.id: (i.balance_minor, i.financial_status, i.source_revision) for i in customer_invoices
        }
        other = db.scalar(
            select(InvoiceSnapshot).where(
                InvoiceSnapshot.tenant_id == invoice.tenant_id,
                InvoiceSnapshot.customer_id != invoice.customer_id,
            )
        )
        assert other
        other_id = other.id
        db.rollback()
        command: dict[str, object] = dict(
            invoice_id=str(invoice.id),
            hold_facts=dict(invoice_ids=[str(invoice.id)], reason_code="customer_contact_stop"),
        )
        result = await callback("operational-stop", "request_collection_hold", command)
        assert await callback("operational-stop", "request_collection_hold", command) == result
        with pytest.raises(HTTPException) as blocked:
            await callback(
                "new-after-stop",
                "create_collection_task",
                dict(invoice_id=str(invoice.id), title="Must not run after customer stop"),
            )
        assert blocked.value.status_code == 403
        row = db.get(VoiceSession, started.id)
        assert row
        call = runtime.context(row)
        binding = runtime.binding(row.tenant_id, row.connection_id)
        assert call.callback_capability is not None
        raw = json.dumps(
            {
                "message": {
                    "type": "end-of-call-report",
                    "call": {
                        "id": row.provider_call_id,
                        "assistantId": str(binding.assistant_id),
                        "assistantOverrides": {
                            "variableValues": {
                                "acoCallCapability": call.callback_capability.get_secret_value()
                            }
                        },
                    },
                    "artifact": {"transcript": "This unapproved utterance must not persist"},
                }
            }
        ).encode()
        stamp = str(int(now().timestamp()))
        headers = {
            "x-timestamp": stamp,
            "x-signature": hmac.new(
                b"synthetic-signing-secret", stamp.encode() + b"." + raw, hashlib.sha256
            ).hexdigest(),
        }
        db.rollback()
        if not release_while_active:
            await runtime.callback(raw, headers)
            await runtime.callback(raw, headers)
            assert await callback("operational-stop", "request_collection_hold", command) == result
            db.rollback()
            completed = db.get(VoiceSession, started.id)
            assert completed is not None and completed.state == "completed"
        for key in financial:
            current = db.get(InvoiceSnapshot, key)
            assert current and pending_contact_stops(db, current)
            assert (current.balance_minor, current.financial_status, current.source_revision) == financial[
                key
            ]
        unrelated = db.get(InvoiceSnapshot, other_id)
        assert unrelated and not pending_contact_stops(db, unrelated)
        assert db.scalar(select(func.count()).select_from(Communication)) == 0
        assert db.scalar(select(func.count()).select_from(Artifact)) == 0
        root = f"/api/workspaces/{world.ctx.tenant_id}"
        db.rollback()
        listed = world.client.get(root + f"/invoices/{invoice.id}/contact-stops")
        assert listed.status_code == 200, listed.text
        captured = listed.json()["items"][0]
        assert captured["source_communication_id"] is None
        assert captured["voice_source"]["native_invocation_id"] == "operational-stop"
        assert captured["pending"] and captured["can_release"] and captured["evidence_complete"]
        reviewed = dict(
            request_id=str(uuid4()),
            expected_action_version=captured["action_version"],
            reviewed_holds=[
                dict(hold_id=h["id"], expected_version=h["version"]) for h in captured["original_holds"]
            ],
            reason="Human reviewed the exact contact-stop facts and all original restrictions",
        )
        endpoint = root + f"/contact-stops/{captured['action_id']}/release"
        stale = world.client.post(endpoint, json=reviewed | {"expected_action_version": 999})
        assert stale.status_code == 409
        released = world.client.post(endpoint, json=reviewed)
        assert released.status_code == 200 and not released.json()["pending"], released.text
        assert world.client.post(endpoint, json=reviewed).json() == released.json()
        db.rollback()
        for key in financial:
            current = db.get(InvoiceSnapshot, key)
            assert current and not pending_contact_stops(db, current)
        assert all(
            h.released_at
            for h in db.scalars(
                select(CollectionHold).where(CollectionHold.reason == "contact_stop_pending_review")
            )
        )
        assert await callback("operational-stop", "request_collection_hold", command) == result
        if release_while_active:
            with pytest.raises(HTTPException):
                await callback(
                    "new-post-disposition",
                    "create_collection_task",
                    dict(invoice_id=str(invoice.id), title="Disposition must not resume this stopped call"),
                )
            await runtime.callback(raw, headers)
            await runtime.callback(raw, headers)
        with pytest.raises(HTTPException):
            await callback(
                "new-post-completion",
                "create_collection_task",
                dict(invoice_id=str(invoice.id), title="No automatic resumption after disposition"),
            )

    detail = await exercise_voice("operational-stop", scenario)
    assert detail and detail.state == "completed" and not detail.transcript_available


@pytest.mark.parametrize("candidate", ["selected", "unselected", "wrong_customer"])
async def test_ambiguous_spoken_commitment_preserves_unassigned_scope_for_review(candidate: str) -> None:
    from aco.features.policies.models import CollectionHold
    from aco.features.promises.models import CustomerScopeReview, PromiseInvoiceLink

    async def scenario(
        world: World, runtime: VoiceRuntime, started: VoiceSessionView, callback: VoiceCallback
    ) -> None:
        db = world.store
        invoice = db.get(InvoiceSnapshot, world.invoice_id)
        assert invoice
        before = (invoice.balance_minor, invoice.financial_status, invoice.source_revision)
        candidate_id = invoice.id
        if candidate != "selected":
            other = db.scalar(
                select(InvoiceSnapshot).where(
                    InvoiceSnapshot.customer_id == invoice.customer_id
                    if candidate == "unselected"
                    else InvoiceSnapshot.customer_id != invoice.customer_id,
                    InvoiceSnapshot.id != invoice.id,
                )
            )
            assert other
            candidate_id = other.id
        command: dict[str, object] = dict(
            invoice_id=str(invoice.id),
            promise_facts=dict(invoice_ids=[], candidate_invoice_ids=[str(candidate_id)], scope="ambiguous"),
        )
        db.rollback()
        if candidate != "selected":
            with pytest.raises(HTTPException):
                await callback("ambiguous-spoken", "record_promise", command)
            db.rollback()
            assert db.scalar(select(func.count()).select_from(PaymentPromise)) == 0
            return
        saved = await callback("ambiguous-spoken", "record_promise", command)
        assert await callback("ambiguous-spoken", "record_promise", command) == saved
        db.rollback()
        row = db.scalar(select(PaymentPromise))
        review = db.scalar(select(CustomerScopeReview))
        assert row and row.status == "NEEDS_CONFIRMATION" and row.candidate_invoice_ids == [str(invoice.id)]
        assert "invoice_scope_unresolved" in row.confirmation_reasons
        assert row.amount is None and row.cutoff_at is None and row.confirmed_at is None
        assert row.source_voice_evidence_id and review and review.customer_id == invoice.customer_id
        assert db.scalar(select(func.count()).select_from(PromiseInvoiceLink)) == 0
        scope_hold_count = db.scalar(
            select(func.count()).select_from(CollectionHold).where(CollectionHold.reason == "scope_review")
        )
        assert scope_hold_count is not None and scope_hold_count > 0
        assert db.scalar(select(func.count()).select_from(Communication)) == 0
        db.refresh(invoice)
        assert (invoice.balance_minor, invoice.financial_status, invoice.source_revision) == before

    await exercise_voice("ambiguous-operational", scenario)
