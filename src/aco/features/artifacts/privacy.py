"""Current-human legal review and deletion requests; retained history and fail-closed quarantine."""

import json
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.features.artifacts.consent_models import ArtifactPrivacyEvent, VoiceArtifactLink
from aco.features.artifacts.models import Artifact, ArtifactPurge
from aco.features.artifacts.privacy_dto import ArtifactPrivacyView, PrivacyCommand, PrivacyEventView
from aco.features.artifacts.service import artifact_view
from aco.features.identity.models import Membership, now
from aco.features.identity.service import IdentityService, Permission


class PrivacyService:
    def __init__(self, db: Session):
        self.db = db

    def row(self, ctx: TenantContext, artifact_id: UUID, permission: Permission) -> Artifact:
        if not isinstance(ctx, TenantContext):
            raise HTTPException(403, "PRIVACY_HUMAN_REQUIRED")
        IdentityService(self.db).authorize(ctx, permission)
        artifact = self.db.scalar(
            select(Artifact)
            .where(Artifact.tenant_id == ctx.tenant_id, Artifact.id == artifact_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if artifact is None:
            raise HTTPException(404, "ARTIFACT_NOT_FOUND")
        return artifact

    def view(self, ctx: TenantContext, artifact_id: UUID) -> ArtifactPrivacyView:
        artifact = self.row(ctx, artifact_id, Permission.INVOICE_READ)
        purge = self.db.scalar(select(ArtifactPurge).where(ArtifactPurge.artifact_id == artifact.id))
        assert purge
        member = self.db.scalar(
            select(Membership).where(
                Membership.tenant_id == ctx.tenant_id, Membership.user_id == ctx.actor_id
            )
        )
        assert member
        link = self.db.scalar(
            select(VoiceArtifactLink).where(
                VoiceArtifactLink.tenant_id == ctx.tenant_id, VoiceArtifactLink.artifact_id == artifact.id
            )
        )
        return ArtifactPrivacyView(
            artifact=artifact_view(artifact),
            version=artifact.version,
            legal_hold_state=artifact.legal_hold_state,
            purge_due_at=purge.due_at,
            purge_status=purge.status,
            failure_code=purge.failure_code,
            provider_purge_status=(
                "NOT_APPLICABLE"
                if link is None
                else "PURGED"
                if link.provider_purged_at
                else "BLOCKED"
                if purge.failure_code == "VOICE_PROVIDER_PURGE_UNAVAILABLE"
                else "PENDING"
            ),
            provider_purge_receipt=link.provider_purge_receipt if link else None,
            can_request_deletion=member.role in {"owner", "finance_manager"},
            can_review=member.role == "owner",
            events=[
                PrivacyEventView(
                    id=e.id,
                    actor_id=e.actor_id,
                    created_at=e.created_at,
                    command=PrivacyCommand.model_validate_json(json.dumps(e.command)),
                )
                for e in self.db.scalars(
                    select(ArtifactPrivacyEvent)
                    .where(
                        ArtifactPrivacyEvent.tenant_id == ctx.tenant_id,
                        ArtifactPrivacyEvent.artifact_id == artifact.id,
                    )
                    .order_by(ArtifactPrivacyEvent.created_at)
                )
            ],
        )

    def record(self, ctx: TenantContext, artifact_id: UUID, command: PrivacyCommand) -> ArtifactPrivacyView:
        permission = (
            Permission.DELETE_REQUEST
            if command.kind in {"DELETE_REQUEST", "QUARANTINE"}
            else Permission.DELETE_AUTHORIZE
        )
        artifact = self.row(ctx, artifact_id, permission)
        old = self.db.scalar(
            select(ArtifactPrivacyEvent).where(
                ArtifactPrivacyEvent.tenant_id == ctx.tenant_id,
                ArtifactPrivacyEvent.request_id == command.request_id,
            )
        )
        payload = command.model_dump(mode="json")
        if old:
            if old.artifact_id != artifact.id or old.command != payload or old.actor_id != ctx.actor_id:
                raise HTTPException(409, "PRIVACY_REPLAY_CONFLICT")
            return self.view(ctx, artifact.id)
        if artifact.version != command.expected_version:
            raise HTTPException(409, "PRIVACY_ARTIFACT_CHANGED")
        if command.review_at is not None and command.review_at <= now():
            raise HTTPException(422, "PRIVACY_REVIEW_DATE_REQUIRED")
        parent = self.db.get(ArtifactPrivacyEvent, command.parent_id) if command.parent_id else None
        if command.kind in {"DELETE_APPROVED", "DELETE_DENIED"}:
            if (
                parent is None
                or parent.tenant_id != ctx.tenant_id
                or parent.artifact_id != artifact.id
                or parent.kind != "DELETE_REQUEST"
            ):
                raise HTTPException(403, "PRIVACY_REQUEST_SCOPE_DENIED")
            if self.db.scalar(
                select(ArtifactPrivacyEvent.id).where(
                    ArtifactPrivacyEvent.tenant_id == ctx.tenant_id,
                    ArtifactPrivacyEvent.parent_id == parent.id,
                )
            ):
                raise HTTPException(409, "PRIVACY_REQUEST_ALREADY_DECIDED")
        if command.kind == "DELETE_APPROVED" and artifact.legal_hold_state != "none":
            raise HTTPException(409, "RETENTION_REVIEW_REQUIRED")
        event = ArtifactPrivacyEvent(
            tenant_id=ctx.tenant_id,
            artifact_id=artifact.id,
            request_id=command.request_id,
            actor_id=ctx.actor_id,
            kind=command.kind,
            parent_id=command.parent_id,
            command=payload,
        )
        self.db.add(event)
        purge = self.db.scalar(
            select(ArtifactPurge).where(ArtifactPurge.artifact_id == artifact.id).with_for_update()
        )
        assert purge
        if command.kind in {"QUARANTINE", "VERIFIED_HOLD"}:
            artifact.legal_hold_state = "review_required" if command.kind == "QUARANTINE" else "verified"
            if artifact.storage_state != "deleted":
                artifact.storage_state = purge.status = "blocked"
                purge.failure_code = "RETENTION_REVIEW_REQUIRED"
        elif command.kind == "RELEASE_HOLD":
            artifact.legal_hold_state = "none"
            if artifact.storage_state != "deleted":
                # Releasing a hold does not restore previously revoked content access.
                artifact.revoked_at = artifact.revoked_at or now()
                artifact.storage_state = purge.status = "purge_pending"
                purge.due_at = min(purge.due_at, now())
                purge.failure_code = None
        elif command.kind == "DELETE_REQUEST":
            # Immediate access withdrawal while the Owner reviews destructive deletion.
            artifact.revoked_at = artifact.revoked_at or now()
            if artifact.storage_state != "deleted":
                artifact.storage_state = purge.status = "blocked"
                purge.failure_code = "OWNER_DELETION_REVIEW_REQUIRED"
        elif command.kind == "DELETE_APPROVED":
            artifact.revoked_at = artifact.revoked_at or now()
            if artifact.storage_state != "deleted":
                artifact.storage_state = purge.status = "purge_pending"
                purge.due_at = min(purge.due_at, now())
                purge.failure_code = None
        artifact.version += 1
        self.db.flush()
        IdentityService(self.db).audit(
            ctx, "artifact.privacy." + command.kind.lower(), artifact.id, {"event_id": str(event.id)}
        )
        return self.view(ctx, artifact.id)
