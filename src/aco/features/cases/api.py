"""Authenticated tenant case reads and evidence-backed mutations."""

from uuid import UUID

from fastapi import APIRouter, HTTPException

from aco.features.cases.dto import (
    CaseMutation,
    CasePage,
    CaseView,
    DispositionReferences,
    EvidenceView,
    InvoiceIssueTransition,
    LinkCaseEvidence,
    TransitionCase,
    UpsertCase,
)
from aco.features.cases.service import CaseService
from aco.features.identity.api import Database, Subject
from aco.features.identity.service import IdentityService

router = APIRouter(prefix="/api/workspaces/{workspace_id}")


@router.get("/cases", response_model=CasePage)
async def list_cases(
    workspace_id: UUID,
    subject: Subject,
    db: Database,
    invoice_id: UUID | None = None,
    customer_id: UUID | None = None,
) -> CasePage:
    return await db.run_sync(
        lambda s: CaseService(s).list(
            IdentityService(s).resolve_context(subject, workspace_id), invoice_id, customer_id
        )
    )


@router.get("/cases/{case_id}", response_model=CaseView)
async def get_case(workspace_id: UUID, case_id: UUID, subject: Subject, db: Database) -> CaseView:
    return await db.run_sync(
        lambda s: CaseService(s).get(IdentityService(s).resolve_context(subject, workspace_id), case_id)
    )


@router.post("/cases", status_code=201, response_model=CaseMutation)
async def upsert_case(
    workspace_id: UUID, command: UpsertCase, subject: Subject, db: Database
) -> CaseMutation:
    return await db.run_sync(
        lambda s: CaseService(s).upsert_case(
            IdentityService(s).resolve_context(subject, workspace_id), command
        )
    )


@router.patch("/cases/{case_id}", response_model=CaseView)
async def transition_case(
    workspace_id: UUID, case_id: UUID, command: TransitionCase, subject: Subject, db: Database
) -> CaseView:
    if case_id != command.case_id:
        raise HTTPException(422, "CASE_ID_MISMATCH")
    return await db.run_sync(
        lambda s: CaseService(s).transition_case(
            IdentityService(s).resolve_context(subject, workspace_id), command
        )
    )


@router.post("/cases/{case_id}/evidence", response_model=EvidenceView)
async def link_evidence(
    workspace_id: UUID, case_id: UUID, command: LinkCaseEvidence, subject: Subject, db: Database
) -> EvidenceView:
    if case_id != command.case_id:
        raise HTTPException(422, "CASE_ID_MISMATCH")
    return await db.run_sync(
        lambda s: CaseService(s).link_case_evidence(
            IdentityService(s).resolve_context(subject, workspace_id), command
        )
    )


@router.post("/cases/{case_id}/invoice-resolution", response_model=CaseView)
async def resolve_invoice(
    workspace_id: UUID, case_id: UUID, command: InvoiceIssueTransition, subject: Subject, db: Database
) -> CaseView:
    if case_id != command.case_id:
        raise HTTPException(422, "CASE_ID_MISMATCH")
    return await db.run_sync(
        lambda s: CaseService(s).set_invoice_issue_state(
            IdentityService(s).resolve_context(subject, workspace_id), command
        )
    )


@router.get("/cases/{case_id}/disposition-references", response_model=DispositionReferences)
async def disposition_references(
    workspace_id: UUID, case_id: UUID, invoice_id: UUID, subject: Subject, db: Database
) -> DispositionReferences:
    return await db.run_sync(
        lambda s: CaseService(s).disposition_references(
            IdentityService(s).resolve_context(subject, workspace_id), case_id, invoice_id
        )
    )
