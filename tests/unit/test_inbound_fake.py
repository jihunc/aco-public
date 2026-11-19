"""Maintained fake model must read the exact captured source before proposing intake work."""

from datetime import date
from uuid import uuid4

import pytest

from aco.features.agent.schemas import InvestigationResult
from aco.features.identity.models import now
from aco.features.tools.schemas import CommunicationFact, InvoiceFact, InvoiceQuery, ReadEvidence, SourceStamp
from aco.integrations.fake.investigation import InvestigationFake
from aco.integrations.ports.llm import LLMInput, LLMRequestPolicy, ReadTool


@pytest.mark.asyncio
@pytest.mark.parametrize("source_present", [True, False])
@pytest.mark.parametrize("invoice_count", [1, 3])
async def test_fake_inbound_requires_exact_captured_message_read(
    source_present: bool, invoice_count: int
) -> None:
    tenant, connection, invoice, source = uuid4(), uuid4(), uuid4(), uuid4()
    source_step = uuid4()
    stamp = SourceStamp(
        provider_connection_id=connection,
        external_id="fixture",
        source_revision="1",
        observed_at=now(),
        synthetic=True,
    )

    async def invoice_read(query: InvoiceQuery) -> ReadEvidence:
        return ReadEvidence(
            step_id=uuid4(),
            invoice_id=query.invoice_id,
            available=True,
            as_of=now(),
            records=[
                InvoiceFact(
                    invoice_id=query.invoice_id,
                    customer_id=uuid4(),
                    number="INV-101",
                    balance_minor=100,
                    currency="AUD",
                    exponent=2,
                    financial_status="unpaid",
                    due_date=date(2026, 1, 1),
                    source=stamp,
                )
            ],
        )

    async def payment_read(query: InvoiceQuery) -> ReadEvidence:
        return ReadEvidence(
            step_id=uuid4(), invoice_id=query.invoice_id, available=True, as_of=now(), records=[]
        )

    async def message_read(query: InvoiceQuery) -> ReadEvidence:
        return ReadEvidence(
            step_id=source_step,
            invoice_id=query.invoice_id,
            available=True,
            as_of=now(),
            records=[
                CommunicationFact(
                    communication_id=source if source_present else uuid4(),
                    direction="incoming",
                    channel="email",
                    event_at=now(),
                    delivery_state="received",
                    synthetic=True,
                    provenance="explicit_text",
                    source_state="stored",
                    excerpt="Review INV-101",
                    text_truncated=False,
                )
            ],
        )

    port = InvestigationFake(
        tenant,
        connection,
        (invoice,) + tuple(uuid4() for _ in range(invoice_count - 1)),
        source_communication_id=source,
    )
    result = await port.run_typed(
        "investigation",
        LLMInput(
            tenant_id=tenant,
            connection_id=connection,
            actor_id=uuid4(),
            correlation_id=uuid4(),
            prompt_version="investigation-v3",
            schema_version="investigation-v3",
            text="User objective: Create an internal intake review task",
        ),
        (
            ReadTool("get_invoice", InvoiceQuery, ReadEvidence, invoice_read),
            ReadTool("get_verified_payments", InvoiceQuery, ReadEvidence, payment_read),
            ReadTool("get_communication_history", InvoiceQuery, ReadEvidence, message_read),
        ),
        InvestigationResult,
        LLMRequestPolicy(max_tool_calls=8),
    )
    assert result.authenticity == "FAKE" and result.provider_attempted is False
    if source_present:
        assert result.output is not None
        assert len(result.output.invoice_findings) == invoice_count
        assert source_step in result.output.invoice_findings[0].evidence_step_ids
        assert result.output.proposals[0].kind == "create_task"
        assert str(source) in result.output.proposals[0].reason
    else:
        assert result.output is None and result.error is not None
