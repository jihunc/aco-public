"""Authenticated communication reads and reviewed synthetic-only execution configuration."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request
from sqlalchemy.orm import Session

from aco.features.communications.dto import (
    CommunicationFilters,
    CommunicationPage,
    CommunicationView,
    DeliveryReconciliation,
    EmailExecutionView,
    ProviderEmailReviewView,
    ReviewEmailExecution,
    ReviewProviderEmailExecution,
    RunDueCommand,
    RunDueResult,
    ThreadView,
)
from aco.features.communications.queries import CommunicationQueries
from aco.features.identity.api import Database, Subject
from aco.features.identity.service import IdentityService, Permission

router = APIRouter(prefix="/api/workspaces/{workspace_id}")


@router.get("/communications", response_model=CommunicationPage)
async def communications(
    workspace_id: UUID, subject: Subject, db: Database, filters: Annotated[CommunicationFilters, Query()]
) -> CommunicationPage:
    return await db.run_sync(
        lambda store: CommunicationQueries(
            store, IdentityService(store).resolve_context(subject, workspace_id)
        ).list(filters)
    )


@router.get("/communications/{communication_id}", response_model=CommunicationView)
async def communication(
    workspace_id: UUID, communication_id: UUID, subject: Subject, db: Database
) -> CommunicationView:
    return await db.run_sync(
        lambda store: CommunicationQueries(
            store, IdentityService(store).resolve_context(subject, workspace_id)
        ).get(communication_id)
    )


@router.get("/communication-threads/{thread_id}", response_model=ThreadView)
async def thread(workspace_id: UUID, thread_id: UUID, subject: Subject, db: Database) -> ThreadView:
    return await db.run_sync(
        lambda store: CommunicationQueries(
            store, IdentityService(store).resolve_context(subject, workspace_id)
        ).thread(thread_id)
    )


@router.get("/email-execution", response_model=EmailExecutionView)
async def execution(
    workspace_id: UUID, request: Request, subject: Subject, db: Database
) -> EmailExecutionView:
    from aco.features.communications.activation import EmailActivationService

    return await db.run_sync(
        lambda store: EmailActivationService(
            store, request.app.state.settings, request.app.state.ports.email
        ).view(IdentityService(store).resolve_context(subject, workspace_id))
    )


@router.put("/email-execution", response_model=EmailExecutionView)
async def review_execution(
    workspace_id: UUID, command: ReviewEmailExecution, request: Request, subject: Subject, db: Database
) -> EmailExecutionView:
    from aco.features.communications.activation import EmailActivationService

    return await db.run_sync(
        lambda store: EmailActivationService(
            store, request.app.state.settings, request.app.state.ports.email
        ).review(IdentityService(store).resolve_context(subject, workspace_id), command)
    )


@router.get("/email-execution/provider", response_model=ProviderEmailReviewView)
async def provider_execution_review(
    workspace_id: UUID, request: Request, subject: Subject, db: Database
) -> ProviderEmailReviewView:
    from aco.features.communications.provider_review import ProviderEmailReviewService

    return await db.run_sync(
        lambda store: ProviderEmailReviewService(
            store, request.app.state.settings, request.app.state.ports.email
        ).view(IdentityService(store).resolve_context(subject, workspace_id))
    )


@router.put("/email-execution/provider", response_model=EmailExecutionView)
async def review_provider_execution(
    workspace_id: UUID,
    command: ReviewProviderEmailExecution,
    request: Request,
    subject: Subject,
    db: Database,
) -> EmailExecutionView:
    from aco.features.communications.provider_review import ProviderEmailReviewService

    return await db.run_sync(
        lambda store: ProviderEmailReviewService(
            store, request.app.state.settings, request.app.state.ports.email
        ).review(IdentityService(store).resolve_context(subject, workspace_id), command)
    )


@router.post("/demo/reminders/run-due", response_model=RunDueResult, status_code=202)
async def run_due(
    workspace_id: UUID, command: RunDueCommand, request: Request, subject: Subject, db: Database
) -> RunDueResult:
    from fastapi import HTTPException

    from aco.features.actions.models import ScheduledAction
    from aco.features.communications.activation import latest, system_enabled, validate_activation
    from aco.features.identity.models import now

    def enqueue(store: Session) -> RunDueResult:
        ctx = IdentityService(store).resolve_context(subject, workspace_id)
        tenant = IdentityService(store).authorize(ctx, Permission.DEMO_RUN_DUE)
        if not tenant.is_demo or not system_enabled(
            request.app.state.settings, request.app.state.ports.email
        ):
            raise HTTPException(403, "SYNTHETIC_EXECUTION_DISABLED")
        activation = latest(store, ctx.tenant_id)
        if activation is None:
            raise HTTPException(403, "EMAIL_EXECUTION_REVIEW_REQUIRED")
        validate_activation(store, tenant, activation)
        action = store.get(ScheduledAction, command.action_id, populate_existing=True)
        if action is None or action.tenant_id != tenant.id:
            raise HTTPException(404, "ACTION_NOT_FOUND")
        if (
            action.kind != "policy_due"
            or action.status != "queued"
            or action.version != command.expected_version
            or action.due_at > now()
        ):
            raise HTTPException(409, "ACTION_NOT_DUE")
        IdentityService(store).audit(ctx, "email.run_due_requested", action.id)
        # Existing committed outbox is consumed by the maintained worker's periodic dispatcher.
        return RunDueResult(action_id=action.id)

    return await db.run_sync(enqueue)


@router.post("/scheduled-actions/{action_id}/reconcile-delivery", response_model=DeliveryReconciliation)
async def reconcile(
    workspace_id: UUID, action_id: UUID, subject: Subject, db: Database
) -> DeliveryReconciliation:
    from aco.features.communications.reconciliation import reconcile_delivery

    return await db.run_sync(
        lambda store: reconcile_delivery(
            store, IdentityService(store).resolve_context(subject, workspace_id), action_id
        )
    )
