"""Selected-invoice reads with fresh run authorization and durable safe evidence."""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from aco.features.accounting.models import Customer, InvoiceSnapshot, VerifiedPaymentAllocation
from aco.features.agent.context import RunAuthority, current_run, selected_invoices
from aco.features.agent.events import finish_step, start_step
from aco.features.agent.models import AgentRun, AgentStep
from aco.features.communications.models import Communication, CommunicationInvoiceLink
from aco.features.identity.models import now
from aco.features.tools.schemas import (
    CaseFact,
    CommunicationFact,
    CustomerFact,
    CustomerInvoiceFact,
    InvoiceFact,
    InvoiceQuery,
    PaymentFact,
    PromiseFact,
    ReadEvidence,
    SourceStamp,
)
from aco.features.tools.task_update import task_facts
from aco.integrations.ports.llm import ReadTool


class ScopedReads:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        authority: RunAuthority,
        boundary: Callable[[Session, AgentRun], Any] | None = None,
    ):
        self.sessions, self.authority = sessions, authority
        self.boundary = boundary

    def _invoice(self, db: Session, query: InvoiceQuery) -> tuple[AgentRun, InvoiceSnapshot]:
        run = current_run(db, self.authority)
        if self.boundary is not None:
            self.boundary(db, run)
        if query.invoice_id not in selected_invoices(db, run):
            raise HTTPException(403, "INVOICE_OUTSIDE_RUN_SCOPE")
        invoice = db.scalar(
            select(InvoiceSnapshot)
            .where(InvoiceSnapshot.id == query.invoice_id, InvoiceSnapshot.tenant_id == run.tenant_id)
            .execution_options(populate_existing=True)
        )
        if invoice is None or not invoice.verified:
            raise HTTPException(409, "VERIFIED_SNAPSHOT_UNAVAILABLE")
        return run, invoice

    @staticmethod
    def stamp(row: Any, external_id: str) -> SourceStamp:
        return SourceStamp(
            provider_connection_id=row.provider_connection_id,
            external_id=external_id,
            source_revision=row.source_revision,
            observed_at=row.observed_at,
            effective_at=getattr(row, "effective_at", None),
            synthetic=row.synthetic,
        )

    def invoice_facts(self, db: Session, invoice: InvoiceSnapshot) -> list[Any]:
        return [
            InvoiceFact(
                invoice_id=invoice.id,
                customer_id=invoice.customer_id,
                number=invoice.number,
                balance_minor=invoice.balance_minor,
                currency=invoice.currency,
                exponent=invoice.exponent,
                financial_status=invoice.financial_status,
                due_date=invoice.due_date,
                source=self.stamp(invoice, invoice.external_invoice_id),
            )
        ]

    def payment_facts(self, db: Session, invoice: InvoiceSnapshot) -> list[Any]:
        rows = db.scalars(
            select(VerifiedPaymentAllocation)
            .where(
                VerifiedPaymentAllocation.tenant_id == invoice.tenant_id,
                VerifiedPaymentAllocation.invoice_id == invoice.id,
                VerifiedPaymentAllocation.provider_connection_id == invoice.provider_connection_id,
            )
            .order_by(VerifiedPaymentAllocation.effective_at.desc(), VerifiedPaymentAllocation.id)
            .limit(51)
        )
        return [
            PaymentFact(
                payment_id=p.id,
                allocation_kind=p.kind,
                minor_units=p.minor_units,
                currency=p.currency,
                source=self.stamp(p, p.external_payment_id + ":" + p.external_allocation_id),
            )
            for p in rows
        ]

    def customer_facts(self, db: Session, invoice: InvoiceSnapshot) -> list[Any]:
        row = db.scalar(
            select(Customer).where(
                Customer.tenant_id == invoice.tenant_id,
                Customer.id == invoice.customer_id,
                Customer.provider_connection_id == invoice.provider_connection_id,
            )
        )
        if row is None:
            raise HTTPException(409, "CUSTOMER_UNAVAILABLE")
        return [
            CustomerFact(customer_id=row.id, name=row.name, source=self.stamp(row, row.external_customer_id))
        ]

    def customer_invoice_facts(self, db: Session, invoice: InvoiceSnapshot) -> list[Any]:
        rows = db.scalars(
            select(InvoiceSnapshot)
            .where(
                InvoiceSnapshot.tenant_id == invoice.tenant_id,
                InvoiceSnapshot.customer_id == invoice.customer_id,
                InvoiceSnapshot.provider_connection_id == invoice.provider_connection_id,
            )
            .order_by(InvoiceSnapshot.number, InvoiceSnapshot.id)
            .limit(51)
            .execution_options(populate_existing=True)
        )
        return [
            CustomerInvoiceFact(invoice=self.invoice_facts(db, row)[0], verified=row.verified) for row in rows
        ]

    def policy_facts(self, db: Session, invoice: InvoiceSnapshot) -> list[Any]:
        from aco.features.tools.context_reads import current_policy, policy_fact

        row = current_policy(db, invoice.tenant_id)
        if row is None:
            raise HTTPException(409, "POLICY_UNAVAILABLE")
        return [policy_fact(row)]

    def contact_facts(self, db: Session, invoice: InvoiceSnapshot) -> list[Any]:
        from aco.features.tools.context_reads import contact_snapshot

        return [contact_snapshot(db, invoice, self.stamp(invoice, invoice.external_invoice_id), now())]

    def communication_facts(self, db: Session, invoice: InvoiceSnapshot) -> list[Any]:
        from aco.features.communications.models import CommunicationSource

        rows = db.execute(
            select(Communication, CommunicationInvoiceLink)
            .join(CommunicationInvoiceLink, CommunicationInvoiceLink.communication_id == Communication.id)
            .where(
                Communication.tenant_id == invoice.tenant_id,
                CommunicationInvoiceLink.tenant_id == invoice.tenant_id,
                CommunicationInvoiceLink.invoice_id == invoice.id,
            )
            .order_by(Communication.event_at.desc(), Communication.id)
            .limit(51)
        )
        return [
            CommunicationFact(
                communication_id=c.id,
                communication_version=c.version,
                source_sha256=db.scalar(
                    select(CommunicationSource.sha256).where(
                        CommunicationSource.tenant_id == invoice.tenant_id,
                        CommunicationSource.communication_id == c.id,
                        CommunicationSource.representation == "provider_original",
                    )
                ),
                direction=c.direction,
                channel=c.channel,
                event_at=c.event_at,
                delivery_state=c.delivery_state,
                synthetic=c.synthetic,
                provenance=link.provenance,
                source_state=c.source_state,
                excerpt=str(c.content.get("text_body", ""))[:2000],
                text_truncated=len(str(c.content.get("text_body", ""))) > 2000,
            )
            for c, link in rows
        ]

    def case_facts(self, db: Session, invoice: InvoiceSnapshot) -> list[Any]:
        from aco.features.cases.models import CaseInvoiceLink, CollectionCase

        rows = db.scalars(
            select(CollectionCase)
            .join(CaseInvoiceLink, CaseInvoiceLink.case_id == CollectionCase.id)
            .where(
                CollectionCase.tenant_id == invoice.tenant_id,
                CaseInvoiceLink.tenant_id == invoice.tenant_id,
                CaseInvoiceLink.invoice_id == invoice.id,
            )
            .order_by(CollectionCase.created_at.desc(), CollectionCase.id)
            .limit(51)
        )
        return [
            CaseFact(
                case_id=r.id,
                customer_id=r.customer_id,
                version=r.version,
                issue_type=r.issue_type,
                claim_key=r.claim_key,
                substance=r.substance,
                status=r.status,
                synthetic=r.synthetic,
            )
            for r in rows
        ]

    def case_evidence_facts(self, db: Session, invoice: InvoiceSnapshot) -> list[Any]:
        from aco.features.tools.case_reads import case_evidence

        return case_evidence(db, invoice)

    def promise_facts(self, db: Session, invoice: InvoiceSnapshot) -> list[Any]:
        from aco.features.promises.models import PaymentPromise, PromiseInvoiceLink

        rows = db.scalars(
            select(PaymentPromise)
            .join(PromiseInvoiceLink, PromiseInvoiceLink.promise_id == PaymentPromise.id)
            .where(
                PaymentPromise.tenant_id == invoice.tenant_id,
                PromiseInvoiceLink.tenant_id == invoice.tenant_id,
                PromiseInvoiceLink.invoice_id == invoice.id,
            )
            .order_by(PaymentPromise.created_at.desc(), PaymentPromise.id)
            .limit(51)
        )
        from aco.features.voice.evidence_dto import VoiceOperationalEvidence
        from aco.features.voice.models import VoiceBusinessEvidence

        facts = []
        for row in rows:
            voice_source = None
            if row.source_voice_evidence_id is not None:
                source = db.get(VoiceBusinessEvidence, row.source_voice_evidence_id)
                if (
                    source is None
                    or source.tenant_id != invoice.tenant_id
                    or source.customer_id != row.customer_id
                    or source.fact_kind != "promise"
                    or source.accounting_connection_id != invoice.provider_connection_id
                ):
                    raise HTTPException(409, "PROMISE_OPERATIONAL_SOURCE_UNAVAILABLE")
                voice_source = VoiceOperationalEvidence(
                    voice_evidence_id=source.id, facts_hash=source.facts_hash
                )
            facts.append(
                PromiseFact(
                    promise_id=row.id,
                    customer_id=row.customer_id,
                    version=row.version,
                    status=row.status,
                    source_kind="voice_operational" if voice_source else "communication",
                    source_communication_id=row.source_communication_id,
                    voice_source=voice_source,
                    source_text=row.source_text[:2000],
                    source_state=row.source_state,
                    synthetic=row.synthetic,
                )
            )
        return facts

    def tool(
        self, name: str, reader: Callable[[Session, InvoiceSnapshot], list[Any]] | None
    ) -> ReadTool[InvoiceQuery, ReadEvidence]:
        async def invoke(query: InvoiceQuery) -> ReadEvidence:
            async with self.sessions.begin() as db:

                def begin(store: Session) -> UUID:
                    run, _ = self._invoice(store, query)
                    if run.last_step_sequence >= 9:
                        raise HTTPException(409, "TOOL_BUDGET_EXHAUSTED")
                    return start_step(
                        store,
                        run,
                        kind="read",
                        tool_name=name,
                        invoice_id=query.invoice_id,
                        arguments=query.model_dump(mode="json"),
                    ).id

                step_id = await db.run_sync(begin)
            try:
                async with self.sessions.begin() as db:

                    def read(store: Session) -> ReadEvidence:
                        run, invoice = self._invoice(store, query)
                        facts = reader(store, invoice) if reader else []
                        result = ReadEvidence(
                            step_id=step_id,
                            invoice_id=invoice.id,
                            available=reader is not None,
                            error_code=None if reader else "CAPABILITY_UNAVAILABLE",
                            records=facts[:50],
                            truncated=len(facts) > 50,
                            as_of=now(),
                        )
                        step = store.get(AgentStep, step_id)
                        assert step is not None
                        finish_step(
                            store,
                            run,
                            step,
                            output=result.model_dump(mode="json"),
                            error=None if reader else "CAPABILITY_UNAVAILABLE",
                        )
                        return result

                    return await db.run_sync(read)
            except Exception:
                # Denial is observable, but never grants a new read after revocation/cancel.
                async with self.sessions.begin() as db:
                    run = await db.get(AgentRun, self.authority.run_id, with_for_update=True)
                    step = await db.get(AgentStep, step_id)
                    if run is not None and step is not None:
                        await db.run_sync(
                            lambda store: finish_step(
                                store, run, step, error="READ_AUTHORITY_OR_SOURCE_UNAVAILABLE"
                            )
                        )
                raise

        return ReadTool(name, InvoiceQuery, ReadEvidence, invoke)

    def tools(self) -> tuple[ReadTool[InvoiceQuery, ReadEvidence], ...]:
        return (
            self.tool("get_invoice", self.invoice_facts),
            self.tool("get_verified_payments", self.payment_facts),
            self.tool("get_payment_records", self.payment_facts),
            self.tool("get_customer", self.customer_facts),
            self.tool("list_customer_invoices", self.customer_invoice_facts),
            self.tool("get_policy_and_contact_status", self.contact_facts),
            self.tool("get_policy", self.policy_facts),
            self.tool("get_collection_policy", self.policy_facts),
            self.tool("get_communication_history", self.communication_facts),
            self.tool("find_existing_cases", self.case_facts),
            self.tool("find_collection_cases", self.case_facts),
            self.tool("search_case_evidence", self.case_evidence_facts),
            self.tool("get_payment_promises", self.promise_facts),
            self.tool("list_collection_tasks", task_facts),
        )
