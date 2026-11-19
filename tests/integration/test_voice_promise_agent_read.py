"""A real signed voice promise remains readable as operational evidence by a later agent."""

import asyncio
from uuid import UUID

import pytest
from action_support import World
from evaluations.test_collection_agent import run_sdk
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from voice_support import VoiceCallback, exercise_voice

from aco.features.accounting.models import InvoiceSnapshot
from aco.features.agent.models import AgentStep
from aco.features.communications.models import Communication
from aco.features.promises.models import PaymentPromise
from aco.features.tools.reads import ScopedReads
from aco.features.voice.dto import VoiceSessionView
from aco.features.voice.models import VoiceBusinessEvidence
from aco.features.voice.runtime import VoiceRuntime


async def test_agent_reads_actual_spoken_promise_without_fabricated_communication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = ScopedReads.promise_facts
    schema_errors: list[str] = []

    def observe(self: ScopedReads, db: Session, invoice: InvoiceSnapshot) -> list[object]:
        try:
            return original(self, db, invoice)
        except ValidationError as exc:
            schema_errors.append(str(exc))
            raise

    monkeypatch.setattr(ScopedReads, "promise_facts", observe)

    async def scenario(
        world: World, runtime: VoiceRuntime, started: VoiceSessionView, callback: VoiceCallback
    ) -> None:
        invoice = world.store.get(InvoiceSnapshot, world.invoice_id)
        assert invoice is not None
        command: dict[str, object] = {
            "invoice_id": str(invoice.id),
            "promise_facts": {
                "invoice_ids": [str(invoice.id)],
                "amount": {"minor_units": 4000, "currency": "AUD", "exponent": 2},
                "due_date": "2026-12-01",
                "timezone": "UTC",
            },
        }
        world.store.rollback()
        await callback("promise-later-agent-read", "record_promise", command)
        world.store.rollback()
        promise = world.store.scalar(select(PaymentPromise))
        assert promise is not None and promise.source_communication_id is None
        source = world.store.get(VoiceBusinessEvidence, promise.source_voice_evidence_id)
        assert source is not None
        promise_id, source_id, facts_hash = promise.id, source.id, source.facts_hash
        world.store.rollback()
        # No parallel access to World/Session: the maintained synchronous harness owns it
        # for this awaited thread, while the current signed voice callback is already committed.
        try:
            saved = await asyncio.to_thread(
                run_sdk, world, [invoice], [], extra_reads=[("get_payment_promises", invoice.id)]
            )
        except AssertionError:
            assert not schema_errors, "Actual signed-source promise read failed: " + "; ".join(schema_errors)
            raise
        assert saved["status"] == "completed", saved
        world.store.rollback()
        step = world.store.scalar(
            select(AgentStep).where(
                AgentStep.run_id == UUID(saved["id"]), AgentStep.tool_name == "get_payment_promises"
            )
        )
        assert step is not None and step.status == "completed" and step.output is not None
        records = step.output["records"]
        assert isinstance(records, list)
        fact = records[0]
        assert isinstance(fact, dict)
        assert fact["promise_id"] == str(promise_id)
        assert fact["source_kind"] == "voice_operational"
        assert fact["source_communication_id"] is None
        assert fact["voice_source"] == {
            "kind": "voice_operational",
            "voice_evidence_id": str(source_id),
            "facts_hash": facts_hash,
        }
        assert fact["source_state"] == "VOICE_OPERATIONAL_METADATA"
        assert "not a transcript or quotation" in fact["source_text"]
        assert world.store.scalar(select(func.count()).select_from(Communication)) == 0
        world.store.refresh(promise)
        assert promise.status == "NEEDS_CONFIRMATION"

    await exercise_voice("agent-spoken-promise-read", scenario)
