"""Current-human synthetic decisions, immutable evidence, and immediate withdrawal."""

import json
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.features.accounting.models import Customer
from aco.features.artifacts.consent_dto import (
    ConsentSourceView,
    RecordVoiceConsent,
    RevokeVoiceConsent,
    VoiceConsentPage,
    VoiceConsentView,
)
from aco.features.artifacts.consent_models import ConsentEvidence, ConsentRevocation, VoiceArtifactLink
from aco.features.artifacts.models import Artifact, ArtifactPurge
from aco.features.identity.models import Actor, Membership, ProviderConnection, Tenant, now
from aco.features.identity.service import IdentityService, Permission
from aco.features.review.approvals import digest

if TYPE_CHECKING:
    from aco.features.voice.runtime import VoiceRuntime


def consent_command(row: ConsentEvidence) -> RecordVoiceConsent:
    return RecordVoiceConsent.model_validate_json(json.dumps(row.command))


def consent_reasons(db: Session, row: ConsentEvidence) -> list[str]:
    """Fresh persisted authority; no caller flags or historical role assertion."""
    reasons: list[str] = []
    command = consent_command(row)
    tenant = db.get(Tenant, row.tenant_id, populate_existing=True)
    member = db.scalar(
        select(Membership)
        .where(Membership.tenant_id == row.tenant_id, Membership.user_id == row.recorded_by)
        .execution_options(populate_existing=True)
    )
    actor = db.get(Actor, row.recorded_by, populate_existing=True)
    if tenant is None or not tenant.is_demo:
        reasons.append("CONSENT_SYNTHETIC_ONLY")
    if (
        member is None
        or not member.active
        or member.role != "owner"
        or member.version != row.membership_version
        or actor is None
        or not actor.active
    ):
        reasons.append("CONSENT_AUTHORITY_CHANGED")
    if command.decision != "GRANTED":
        reasons.append("CONSENT_DECLINED")
    if now() >= row.expires_at:
        reasons.append("CONSENT_EXPIRED")
    if db.scalar(
        select(ConsentRevocation.id).where(
            ConsentRevocation.tenant_id == row.tenant_id, ConsentRevocation.consent_id == row.id
        )
    ):
        reasons.append("CONSENT_REVOKED")
    for key, version, kind in (
        (row.voice_connection_id, row.voice_connection_version, "voice"),
        (row.storage_connection_id, row.storage_connection_version, "storage"),
    ):
        if key is None:
            continue
        connection = db.get(ProviderConnection, key, populate_existing=True)
        if (
            connection is None
            or connection.tenant_id != row.tenant_id
            or connection.provider_type != kind
            or connection.status != "READY"
            or connection.version != version
        ):
            reasons.append("CONSENT_CONNECTION_CHANGED")
    return list(dict.fromkeys(reasons))


def voice_read_allowed(db: Session, artifact: Artifact) -> bool:
    if artifact.capture_class != "synthetic_consented_voice":
        return True
    link = db.scalar(
        select(VoiceArtifactLink).where(
            VoiceArtifactLink.tenant_id == artifact.tenant_id, VoiceArtifactLink.artifact_id == artifact.id
        )
    )
    consent = db.get(ConsentEvidence, link.consent_id, populate_existing=True) if link else None
    if (
        link is None
        or consent is None
        or consent.tenant_id != artifact.tenant_id
        or consent_reasons(db, consent)
    ):
        return False
    scopes = consent_command(consent).scopes
    return scopes.private_storage and (scopes.transcript if link.kind == "transcript" else scopes.playback)


class ConsentService:
    def __init__(self, db: Session, runtime: "VoiceRuntime | None", *, storage_available: bool = False):
        self.db, self.runtime = db, runtime
        self.storage_available = storage_available

    def human(self, ctx: TenantContext, permission: Permission) -> Tenant:
        if not isinstance(ctx, TenantContext):
            raise HTTPException(403, "CONSENT_HUMAN_REQUIRED")
        return IdentityService(self.db).authorize(ctx, permission)

    def configuration_reasons(self, tenant: Tenant) -> list[str]:
        if not tenant.is_demo:
            return ["CONSENT_SYNTHETIC_ONLY"]
        if (
            self.runtime is None
            or not self.runtime.enabled
            or not self.runtime.setup_enabled
            or self.runtime.environment != "local"
        ):
            return ["CONSENT_LOCAL_PROFILE_DISABLED"]
        return []

    def view(self, row: ConsentEvidence) -> VoiceConsentView:
        revoked = self.db.scalar(
            select(ConsentRevocation).where(
                ConsentRevocation.tenant_id == row.tenant_id, ConsentRevocation.consent_id == row.id
            )
        )
        reasons = consent_reasons(self.db, row)
        return VoiceConsentView(
            id=row.id,
            version=row.version,
            command=consent_command(row),
            recorded_by=row.recorded_by,
            recorded_at=row.created_at,
            material_hash=row.material_hash,
            revoked_at=revoked.created_at if revoked else None,
            revocation_reason=str(revoked.command["reason"]) if revoked else None,
            active=not reasons,
            reasons=reasons,
        )

    def list(self, ctx: TenantContext) -> VoiceConsentPage:
        tenant = self.human(ctx, Permission.INVOICE_READ)
        reasons = self.configuration_reasons(tenant)
        member = self.db.scalar(
            select(Membership).where(
                Membership.tenant_id == ctx.tenant_id, Membership.user_id == ctx.actor_id
            )
        )
        if member is None or member.role != "owner":
            reasons.append("CONSENT_OWNER_REQUIRED")
        rows = list(
            self.db.scalars(
                select(ProviderConnection).where(
                    ProviderConnection.tenant_id == ctx.tenant_id, ProviderConnection.status == "READY"
                )
            )
        )
        voice = [
            ConsentSourceView(id=row.id, label="Synthetic offline voice")
            for row in rows
            if row.provider_type == "voice" and row.provider == "vapi"
        ]
        storage = [
            ConsentSourceView(id=row.id, label="Private application storage")
            for row in rows
            if row.provider_type == "storage"
        ]
        storage_reasons = list(reasons)
        if not self.storage_available:
            storage_reasons.append("APP_STORAGE_RUNTIME_REQUIRED")
        if not voice:
            reasons.append("CONSENT_VOICE_CONNECTION_REQUIRED")
        return VoiceConsentPage(
            can_prepare_storage=not storage_reasons,
            storage_setup_reasons=storage_reasons,
            items=[
                self.view(row)
                for row in self.db.scalars(
                    select(ConsentEvidence)
                    .where(ConsentEvidence.tenant_id == ctx.tenant_id)
                    .order_by(ConsentEvidence.created_at.desc())
                )
            ],
            can_record=not reasons,
            reasons=reasons,
            voice_connections=voice,
            storage_connections=storage,
        )

    def record(self, ctx: TenantContext, command: RecordVoiceConsent) -> VoiceConsentView:
        tenant = self.human(ctx, Permission.SETTINGS)
        reasons = self.configuration_reasons(tenant)
        if reasons:
            raise HTTPException(403, reasons[0])
        old = self.db.scalar(
            select(ConsentEvidence).where(
                ConsentEvidence.tenant_id == ctx.tenant_id, ConsentEvidence.request_id == command.request_id
            )
        )
        payload = command.model_dump(mode="json")
        if old:
            if old.command != payload:
                raise HTTPException(409, "CONSENT_REPLAY_CONFLICT")
            return self.view(old)
        if not now() < command.expires_at <= now() + timedelta(days=30):
            raise HTTPException(422, "CONSENT_EXPIRY_OUTSIDE_POLICY")
        customer = self.db.get(Customer, command.customer_id)
        if customer is None or customer.tenant_id != ctx.tenant_id or not customer.synthetic:
            raise HTTPException(403, "CONSENT_CUSTOMER_SCOPE_DENIED")
        voice = self.db.get(ProviderConnection, command.voice_connection_id)
        if (
            voice is None
            or voice.tenant_id != ctx.tenant_id
            or voice.provider != "vapi"
            or voice.provider_type != "voice"
            or voice.status != "READY"
        ):
            raise HTTPException(403, "CONSENT_VOICE_CONNECTION_REQUIRED")
        assert self.runtime is not None
        binding = self.runtime.binding(ctx.tenant_id, voice.id)
        if voice.encrypted_credentials_ref != binding.credential_revision:
            raise HTTPException(403, "CONSENT_CONNECTION_CHANGED")
        storage = (
            self.db.get(ProviderConnection, command.storage_connection_id)
            if command.storage_connection_id
            else None
        )
        if command.storage_connection_id and (
            storage is None
            or storage.tenant_id != ctx.tenant_id
            or storage.provider_type != "storage"
            or storage.status != "READY"
        ):
            raise HTTPException(403, "CONSENT_STORAGE_CONNECTION_REQUIRED")
        member = self.db.scalar(
            select(Membership).where(
                Membership.tenant_id == ctx.tenant_id, Membership.user_id == ctx.actor_id
            )
        )
        assert member
        row = ConsentEvidence(
            tenant_id=ctx.tenant_id,
            request_id=command.request_id,
            customer_id=customer.id,
            accounting_connection_id=customer.provider_connection_id,
            voice_connection_id=voice.id,
            voice_connection_version=voice.version,
            storage_connection_id=storage.id if storage else None,
            storage_connection_version=storage.version if storage else None,
            recorded_by=ctx.actor_id,
            membership_version=member.version,
            tenant_version=tenant.version,
            command=payload,
            material_hash=digest(payload),
            expires_at=command.expires_at,
        )
        self.db.add(row)
        self.db.flush()
        IdentityService(self.db).audit(
            ctx,
            "consent.synthetic_recorded",
            row.id,
            {"material_hash": row.material_hash, "decision": command.decision},
        )
        return self.view(row)

    def revoke(self, ctx: TenantContext, key: UUID, command: RevokeVoiceConsent) -> VoiceConsentView:
        self.human(ctx, Permission.SETTINGS)
        row = self.db.scalar(
            select(ConsentEvidence).where(
                ConsentEvidence.tenant_id == ctx.tenant_id, ConsentEvidence.id == key
            )
        )
        if row is None:
            raise HTTPException(404, "CONSENT_NOT_FOUND")
        old = self.db.scalar(
            select(ConsentRevocation).where(
                ConsentRevocation.tenant_id == ctx.tenant_id,
                (ConsentRevocation.consent_id == key) | (ConsentRevocation.request_id == command.request_id),
            )
        )
        if old:
            if old.command != command.model_dump(mode="json") or old.consent_id != key:
                raise HTTPException(409, "CONSENT_REVOCATION_CONFLICT")
            return self.view(row)
        if row.version != command.expected_version:
            raise HTTPException(409, "CONSENT_VERSION_CHANGED")
        self.db.add(
            ConsentRevocation(
                tenant_id=ctx.tenant_id,
                consent_id=row.id,
                request_id=command.request_id,
                actor_id=ctx.actor_id,
                command=command.model_dump(mode="json"),
            )
        )
        self.db.flush()
        for artifact in self.db.scalars(
            select(Artifact)
            .join(VoiceArtifactLink, VoiceArtifactLink.artifact_id == Artifact.id)
            .where(VoiceArtifactLink.tenant_id == ctx.tenant_id, VoiceArtifactLink.consent_id == row.id)
            .with_for_update(of=Artifact)
        ):
            artifact.consent_revoked = True
            artifact.revoked_at = artifact.revoked_at or now()
            if artifact.storage_state != "deleted":
                artifact.storage_state = "blocked" if artifact.legal_hold_state != "none" else "purge_pending"
                purge = self.db.scalar(select(ArtifactPurge).where(ArtifactPurge.artifact_id == artifact.id))
                assert purge
                purge.due_at = min(purge.due_at, now())
                purge.status, purge.reason = artifact.storage_state, "consent_withdrawn"
                purge.failure_code = None
        IdentityService(self.db).audit(ctx, "consent.withdrawn", row.id)
        return self.view(row)
