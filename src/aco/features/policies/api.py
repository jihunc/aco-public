"""Authenticated policy configuration; dispatch authority is never accepted over HTTP."""

from uuid import UUID

from fastapi import APIRouter
from pydantic import AwareDatetime
from sqlalchemy.orm import Session

from aco.features.identity.api import Database, Subject
from aco.features.identity.models import now
from aco.features.identity.service import IdentityService
from aco.features.policies.contact_stop_review import (
    ContactStopPage,
    ContactStopReviewService,
    ContactStopView,
    ReleaseContactStop,
)
from aco.features.policies.dto import (
    NextAction,
    PauseCommand,
    PauseState,
    PolicyArchive,
    PolicyArchiveResult,
    PolicyPreview,
    PolicyRecompute,
    PolicySave,
    PolicyVersion,
    RecipientSave,
    RecipientView,
)
from aco.features.policies.service import PolicyService

router = APIRouter(prefix="/api/workspaces/{workspace_id}")


@router.get("/policies", response_model=list[PolicyVersion])
async def policies(workspace_id: UUID, subject: Subject, db: Database) -> list[PolicyVersion]:
    return await db.run_sync(
        lambda store: PolicyService(store).list_policies(
            IdentityService(store).resolve_context(subject, workspace_id)
        )
    )


@router.post("/policies", response_model=PolicyVersion, status_code=201)
async def create(workspace_id: UUID, command: PolicySave, subject: Subject, db: Database) -> PolicyVersion:
    return await db.run_sync(
        lambda store: PolicyService(store).save_policy(
            IdentityService(store).resolve_context(subject, workspace_id), command, command.expected_version
        )
    )


@router.get("/policies/{policy_id}", response_model=PolicyVersion)
async def read(workspace_id: UUID, policy_id: UUID, subject: Subject, db: Database) -> PolicyVersion:
    def load(store: Session) -> PolicyVersion:
        service = PolicyService(store)
        policy = service.policy(IdentityService(store).resolve_context(subject, workspace_id), policy_id)
        return service.view(policy, service.latest(policy))

    return await db.run_sync(load)


@router.put("/policies/{policy_id}", response_model=PolicyVersion)
async def update(
    workspace_id: UUID, policy_id: UUID, command: PolicySave, subject: Subject, db: Database
) -> PolicyVersion:
    return await db.run_sync(
        lambda store: PolicyService(store).save_policy(
            IdentityService(store).resolve_context(subject, workspace_id),
            command,
            command.expected_version,
            policy_id,
        )
    )


@router.get("/policies/{policy_id}/versions", response_model=list[PolicyVersion])
async def versions(
    workspace_id: UUID, policy_id: UUID, subject: Subject, db: Database
) -> list[PolicyVersion]:
    return await db.run_sync(
        lambda store: PolicyService(store).history(
            IdentityService(store).resolve_context(subject, workspace_id), policy_id
        )
    )


@router.get("/policies/{policy_id}/preview", response_model=PolicyPreview)
async def preview(
    workspace_id: UUID, policy_id: UUID, subject: Subject, db: Database, as_of: AwareDatetime | None = None
) -> PolicyPreview:
    def load(store: Session) -> PolicyPreview:
        service = PolicyService(store)
        ctx = IdentityService(store).resolve_context(subject, workspace_id)
        policy = service.policy(ctx, policy_id)
        return service.preview_policy(ctx, service.view(policy, service.latest(policy)), as_of or now())

    return await db.run_sync(load)


@router.post("/policies/{policy_id}/archive", response_model=PolicyArchiveResult)
async def archive(
    workspace_id: UUID, policy_id: UUID, command: PolicyArchive, subject: Subject, db: Database
) -> PolicyArchiveResult:
    return await db.run_sync(
        lambda store: PolicyService(store).archive_policy(
            IdentityService(store).resolve_context(subject, workspace_id),
            policy_id,
            command.expected_version,
            command,
        )
    )


@router.get("/policy-recipients", response_model=list[RecipientView])
async def recipients(workspace_id: UUID, subject: Subject, db: Database) -> list[RecipientView]:
    return await db.run_sync(
        lambda store: PolicyService(store).recipients(
            IdentityService(store).resolve_context(subject, workspace_id)
        )
    )


@router.put("/policy-recipients/{contact_id}", response_model=RecipientView)
async def recipient(
    workspace_id: UUID, contact_id: UUID, command: RecipientSave, subject: Subject, db: Database
) -> RecipientView:
    return await db.run_sync(
        lambda store: PolicyService(store).save_recipient(
            IdentityService(store).resolve_context(subject, workspace_id), contact_id, command
        )
    )


@router.post("/automation/pause", response_model=PauseState)
async def pause(workspace_id: UUID, command: PauseCommand, subject: Subject, db: Database) -> PauseState:
    return await db.run_sync(
        lambda store: PolicyService(store).set_outbound_pause(
            IdentityService(store).resolve_context(subject, workspace_id), command.scope, command.paused
        )
    )


@router.post("/policies/{policy_id}/recompute", response_model=list[NextAction])
async def recompute(
    workspace_id: UUID, policy_id: UUID, command: PolicyRecompute, subject: Subject, db: Database
) -> list[NextAction]:
    return await db.run_sync(
        lambda store: PolicyService(store).recompute_policy(
            IdentityService(store).resolve_context(subject, workspace_id), policy_id, command.expected_version
        )
    )


@router.get("/invoices/{invoice_id}/contact-stops", response_model=ContactStopPage)
async def contact_stops(
    workspace_id: UUID, invoice_id: UUID, subject: Subject, db: Database
) -> ContactStopPage:
    return await db.run_sync(
        lambda store: ContactStopReviewService(store).list(
            IdentityService(store).resolve_context(subject, workspace_id), invoice_id
        )
    )


@router.post("/contact-stops/{action_id}/release", response_model=ContactStopView)
async def release_contact_stop(
    workspace_id: UUID, action_id: UUID, command: ReleaseContactStop, subject: Subject, db: Database
) -> ContactStopView:
    return await db.run_sync(
        lambda store: ContactStopReviewService(store).release(
            IdentityService(store).resolve_context(subject, workspace_id), action_id, command
        )
    )
