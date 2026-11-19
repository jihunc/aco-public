"""Optional exact case context for internal tasks; never issue-resolution authority."""

import json
from uuid import UUID

from fastapi import HTTPException
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.types import StrictModel
from aco.features.accounting.models import InvoiceSnapshot
from aco.features.agent.context import selected_invoices
from aco.features.agent.models import AgentRun, AgentStep
from aco.features.cases.models import CaseInvoiceLink, CollectionCase
from aco.features.tools.read_names import equivalent_read_names
from aco.features.tools.schemas import CaseFact, ReadEvidence


class TaskCaseBinding(StrictModel):
    case_id: UUID
    case_version: int = Field(ge=1)


def current_task_case(
    db: Session, run: AgentRun, invoice_id: UUID, binding: TaskCaseBinding, evidence_ids: list[UUID]
) -> dict[str, object]:
    case = db.scalar(
        select(CollectionCase)
        .where(CollectionCase.id == binding.case_id, CollectionCase.tenant_id == run.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if case is None or case.version != binding.case_version:
        raise HTTPException(409, "TASK_CASE_CHANGED")
    scope = set(
        db.scalars(
            select(CaseInvoiceLink.invoice_id).where(
                CaseInvoiceLink.tenant_id == run.tenant_id, CaseInvoiceLink.case_id == case.id
            )
        )
    )
    invoices = list(
        db.scalars(
            select(InvoiceSnapshot)
            .where(InvoiceSnapshot.tenant_id == run.tenant_id, InvoiceSnapshot.id.in_(scope))
            .execution_options(populate_existing=True)
        )
    )
    if (
        invoice_id not in scope
        or not scope.issubset(selected_invoices(db, run))
        or len(invoices) != len(scope)
        or any(
            i.customer_id != case.customer_id or i.provider_connection_id != case.provider_connection_id
            for i in invoices
        )
    ):
        raise HTTPException(403, "TASK_CASE_OUTSIDE_RUN_SCOPE")
    reads = db.scalars(
        select(AgentStep).where(
            AgentStep.tenant_id == run.tenant_id,
            AgentStep.run_id == run.id,
            AgentStep.invoice_id == invoice_id,
            AgentStep.id.in_(evidence_ids),
            AgentStep.kind == "read",
            AgentStep.status == "completed",
            AgentStep.tool_name.in_(equivalent_read_names("find_existing_cases")),
        )
    )
    if not any(
        isinstance(fact, CaseFact)
        and fact.case_id == case.id
        and fact.version == case.version
        and fact.customer_id == case.customer_id
        for step in reads
        for fact in ReadEvidence.model_validate_json(json.dumps(step.output)).records
    ):
        raise HTTPException(403, "TASK_CASE_EVIDENCE_REQUIRED")
    return {
        **binding.model_dump(mode="json"),
        "customer_id": str(case.customer_id),
        "provider_connection_id": str(case.provider_connection_id),
        "invoice_ids": sorted(map(str, scope)),
    }
