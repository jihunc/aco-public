"""Current human membership, CSRF and strict evidence-bearing promise commands."""

from uuid import UUID

from fastapi import APIRouter, HTTPException

from aco.features.identity.api import Database, Subject
from aco.features.identity.service import IdentityService
from aco.features.promises.dto import (
    CancelPromise,
    ConfirmPromise,
    PromiseCheckPlan,
    PromisePage,
    PromiseView,
    RecordPromise,
)
from aco.features.promises.service import PromiseService

router = APIRouter(prefix="/api/workspaces/{workspace_id}")


@router.get("/promises", response_model=PromisePage)
async def list_promises(
    workspace_id: UUID,
    subject: Subject,
    db: Database,
    invoice_id: UUID | None = None,
    customer_id: UUID | None = None,
) -> PromisePage:
    return await db.run_sync(
        lambda s: PromiseService(s).list(
            IdentityService(s).resolve_context(subject, workspace_id), invoice_id, customer_id
        )
    )


@router.get("/promises/{promise_id}", response_model=PromiseView)
async def get_promise(workspace_id: UUID, promise_id: UUID, subject: Subject, db: Database) -> PromiseView:
    return await db.run_sync(
        lambda s: PromiseService(s).get(IdentityService(s).resolve_context(subject, workspace_id), promise_id)
    )


@router.post("/promises", status_code=201, response_model=PromiseView)
async def record_promise(
    workspace_id: UUID, command: RecordPromise, subject: Subject, db: Database
) -> PromiseView:
    return await db.run_sync(
        lambda s: PromiseService(s).record_promise(
            IdentityService(s).resolve_context(subject, workspace_id), command
        )
    )


@router.post("/promises/{promise_id}/confirm", response_model=PromiseView)
async def confirm_promise(
    workspace_id: UUID, promise_id: UUID, command: ConfirmPromise, subject: Subject, db: Database
) -> PromiseView:
    if promise_id != command.promise_id:
        raise HTTPException(422, "PROMISE_ID_MISMATCH")
    return await db.run_sync(
        lambda s: PromiseService(s).confirm_promise(
            IdentityService(s).resolve_context(subject, workspace_id), command
        )
    )


@router.post("/promises/{promise_id}/cancel", response_model=PromiseView)
async def cancel_promise(
    workspace_id: UUID, promise_id: UUID, command: CancelPromise, subject: Subject, db: Database
) -> PromiseView:
    if promise_id != command.promise_id:
        raise HTTPException(422, "PROMISE_ID_MISMATCH")
    return await db.run_sync(
        lambda s: PromiseService(s).cancel_promise(
            IdentityService(s).resolve_context(subject, workspace_id), command
        )
    )


@router.get("/promises/{promise_id}/check-plans", response_model=list[PromiseCheckPlan])
async def check_plans(
    workspace_id: UUID, promise_id: UUID, subject: Subject, db: Database
) -> list[PromiseCheckPlan]:
    return await db.run_sync(
        lambda s: PromiseService(s).prepare_promise_checks(
            IdentityService(s).resolve_context(subject, workspace_id), promise_id
        )
    )
