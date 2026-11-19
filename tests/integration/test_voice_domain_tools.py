"""Actual approved mocked call, signed tools and canonical domain mutations; no transcript."""

import hashlib
import hmac
import json

import pytest
from action_support import World
from case_promise_support import communication
from fastapi import HTTPException
from sqlalchemy import func, select
from voice_support import VoiceCallback, exercise_voice

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.cases.models import CaseEvidence, CollectionCase
from aco.features.communications.models import CommunicationInvoiceLink
from aco.features.identity.models import now
from aco.features.promises.models import PaymentPromise
from aco.features.voice.dto import VoiceSessionView
from aco.features.voice.models import VoiceInvocation, VoiceSession
from aco.features.voice.runtime import VoiceRuntime


@pytest.mark.parametrize(
    "boundary",
    [
        "success",
        "unread",
        "span",
        "customer",
        "unselected",
        "ambiguous",
        "untrusted_provenance",
        "existing_case_outside_scope",
    ],
)
async def test_signed_call_reuses_read_incoming_source_for_actual_case_and_promise(boundary: str) -> None:
    async def scenario(
        world: World, runtime: VoiceRuntime, started: VoiceSessionView, callback: VoiceCallback
    ) -> None:
        db = world.store
        invoice = db.get(InvoiceSnapshot, world.invoice_id)
        assert invoice
        before = (invoice.balance_minor, invoice.financial_status, invoice.source_revision)
        original = communication(
            db,
            world.ctx.tenant_id,
            [invoice],
            "Explicit synthetic incoming source: invoice amount disputed; AUD 40 promised by 2026-12-01 UTC.",
        )
        source = dict(communication_id=str(original.id), excerpt=original.content["text_body"], offset=0)
        if boundary == "existing_case_outside_scope":
            from aco.features.cases.dto import UpsertCase
            from aco.features.cases.service import CaseService

            other = db.scalar(
                select(InvoiceSnapshot).where(
                    InvoiceSnapshot.customer_id == invoice.customer_id, InvoiceSnapshot.id != invoice.id
                )
            )
            assert other
            broader = communication(
                db, world.ctx.tenant_id, [invoice, other], "Explicit earlier synthetic multi-invoice dispute"
            )
            CaseService(db).upsert_case(
                world.ctx,
                UpsertCase.model_validate_json(
                    json.dumps(
                        dict(
                            customer_id=str(invoice.customer_id),
                            invoice_ids=[str(invoice.id), str(other.id)],
                            evidence=dict(
                                communication_id=str(broader.id),
                                offset=0,
                                excerpt=broader.content["text_body"],
                            ),
                            issue_type="wrong_amount",
                            claim_key="voice-reviewed-original-rate",
                            substance="Existing genuine synthetic multi-invoice case",
                        )
                    )
                ),
            )
            db.commit()
        if boundary != "unread":
            await callback("history-1", "get_communication_history", dict(invoice_id=str(invoice.id)))
        if boundary == "existing_case_outside_scope":
            await callback("cases-1", "find_existing_cases", dict(invoice_id=str(invoice.id)))
        case_command = dict(
            customer_id=str(invoice.customer_id),
            invoice_ids=[str(invoice.id)],
            evidence=source,
            issue_type="wrong_amount",
            claim_key="voice-reviewed-original-rate",
            substance="Review disputed amount from the actually read incoming original",
        )
        if boundary == "span":
            case_command["evidence"] = source | {"excerpt": "An unread forged span"}
        elif boundary == "customer":
            other = db.scalar(
                select(InvoiceSnapshot).where(InvoiceSnapshot.customer_id != invoice.customer_id)
            )
            assert other
            case_command["customer_id"] = str(other.customer_id)
        elif boundary == "unselected":
            other = db.scalar(select(InvoiceSnapshot).where(InvoiceSnapshot.id != invoice.id))
            assert other
            case_command["invoice_ids"] = [str(other.id)]
        elif boundary in {"ambiguous", "untrusted_provenance"}:
            db.add(
                CommunicationInvoiceLink(
                    tenant_id=world.ctx.tenant_id,
                    communication_id=original.id,
                    invoice_id=invoice.id,
                    claim_or_context_key="explicit-negative-ambiguity",
                    confidence="0.5" if boundary == "ambiguous" else "contextual_unverified_sender",
                    provenance="explicit_reference" if boundary == "ambiguous" else "untrusted-claim",
                    evidence_excerpt=source["excerpt"],
                    evidence_offset=0,
                    classification="synthetic_negative_fixture",
                )
            )
            db.commit()
        arguments: dict[str, object] = dict(invoice_id=str(invoice.id), case_command=case_command)
        db.rollback()
        if boundary != "success":
            with pytest.raises(HTTPException):
                await callback("case-1", "upsert_case", arguments)
            db.rollback()
            assert db.scalar(select(func.count()).select_from(CollectionCase)) == (
                1 if boundary == "existing_case_outside_scope" else 0
            )
            return
        case_result = await callback("case-1", "upsert_case", arguments)
        assert await callback("case-1", "upsert_case", arguments) == case_result
        promise_args: dict[str, object] = dict(
            invoice_id=str(invoice.id),
            promise_command=dict(
                customer_id=str(invoice.customer_id),
                invoice_ids=[str(invoice.id)],
                evidence=source,
                due_date="2026-12-01",
                timezone="UTC",
                amount=dict(minor_units=4000, currency="AUD", exponent=2),
            ),
        )
        promised = await callback("promise-1", "record_promise", promise_args)
        assert await callback("promise-1", "record_promise", promise_args) == promised
        with pytest.raises(HTTPException, match="VOICE_TARGET_NOT_READ"):
            await callback("unread-existing-case", "upsert_case", arguments)
        await callback("cases-1", "find_existing_cases", dict(invoice_id=str(invoice.id)))
        repeated_case = await callback("case-2", "upsert_case", arguments)
        assert repeated_case
        db.rollback()
        case = db.scalar(select(CollectionCase))
        commitment = db.scalar(select(PaymentPromise))
        assert case and case.status == "OPEN" and case.issue_type == "wrong_amount"
        assert (
            commitment
            and commitment.status == "NEEDS_CONFIRMATION"
            and commitment.source_communication_id == original.id
        )
        assert db.scalar(select(func.count()).select_from(CaseEvidence)) == 1
        row = db.get(VoiceSession, started.id)
        assert row and row.provider_call_id
        token = runtime.context(row).callback_capability
        assert token
        raw = json.dumps(
            dict(
                message=dict(
                    type="end-of-call-report",
                    call=dict(
                        id=row.provider_call_id,
                        assistantId=str(runtime.binding(row.tenant_id, row.connection_id).assistant_id),
                        assistantOverrides=dict(
                            variableValues=dict(acoCallCapability=token.get_secret_value())
                        ),
                    ),
                    artifact=dict(transcript="Unapproved speech must never persist"),
                )
            )
        ).encode()
        stamp = str(int(now().timestamp()))
        headers = {
            "x-timestamp": stamp,
            "x-signature": hmac.new(
                b"synthetic-signing-secret", stamp.encode() + b"." + raw, hashlib.sha256
            ).hexdigest(),
        }
        db.rollback()
        await runtime.callback(raw, headers)
        await runtime.callback(raw, headers)
        assert await callback("case-1", "upsert_case", arguments) == case_result
        assert await callback("promise-1", "record_promise", promise_args) == promised
        with pytest.raises(HTTPException):
            await callback("late-case", "upsert_case", arguments)
        db.rollback()
        assert db.scalar(select(func.count()).select_from(CollectionCase)) == 1
        assert db.scalar(select(func.count()).select_from(PaymentPromise)) == 1
        assert db.scalar(select(func.count()).select_from(VoiceInvocation)) == 6
        db.refresh(invoice)
        assert (invoice.balance_minor, invoice.financial_status, invoice.source_revision) == before

    detail = await exercise_voice("domain", scenario)
    assert detail and not detail.transcript_available and not detail.recording_available
    if boundary == "success":
        assert {outcome.record_kind for outcome in detail.outcomes} == {"case", "promise"}
        assert all(outcome.action_id and outcome.changed_ids for outcome in detail.outcomes)
