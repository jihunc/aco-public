"""Exact current-human disposition; no recipient authority or automatic contact resumption."""

import hashlib
import json
from datetime import datetime
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.features.accounting.models import InvoiceSnapshot
from aco.features.actions.models import ScheduledAction
from aco.features.cases.service import release_hold
from aco.features.identity.models import AuditEvent
from aco.features.identity.service import IdentityService, Permission
from aco.features.policies.contact_stop import REASON, contact_stop_source, stop_actions
from aco.features.policies.models import CollectionHold
from aco.features.voice.evidence_dto import VoiceEvidenceView


class ReviewedHold(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hold_id: UUID
    expected_version: int = Field(ge=1, strict=True)


class ReleaseContactStop(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    expected_action_version: int = Field(ge=1, strict=True)
    reviewed_holds: list[ReviewedHold] = Field(min_length=1, max_length=10000)
    reason: str = Field(min_length=10, max_length=1000)

    @field_validator("reason")
    @classmethod
    def substantive_reason(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 10:
            raise ValueError("A substantive human disposition reason is required")
        return value


class ContactStopHoldView(BaseModel):
    id: UUID
    invoice_id: UUID
    version: int
    released_at: datetime | None
    release_evidence: str | None


class ContactStopView(BaseModel):
    action_id: UUID
    action_version: int
    customer_id: UUID
    accounting_connection_id: UUID
    source_communication_id: UUID | None
    voice_source: VoiceEvidenceView | None = None
    source_excerpt: str
    original_holds: list[ContactStopHoldView]
    evidence_complete: bool
    pending: bool
    can_release: bool


class ContactStopPage(BaseModel):
    items: list[ContactStopView]


class ContactStopReviewService:
    def __init__(self, db: Session):
        self.db = db

    def _human(self, ctx: TenantContext, permission: Permission) -> None:
        if not isinstance(ctx, TenantContext):
            raise HTTPException(403, "CURRENT_HUMAN_REQUIRED")
        IdentityService(self.db).authorize(ctx, permission)

    def _view(self, ctx: TenantContext, action: ScheduledAction) -> ContactStopView:
        from aco.features.voice.evidence import source_view

        source = contact_stop_source(self.db, action)
        if source is None:
            raise HTTPException(409, "CONTACT_STOP_EVIDENCE_UNAVAILABLE")
        customer, source_id, voice_id, marker, excerpt = source
        ids = (action.result or {}).get("changed_ids")
        complete = isinstance(ids, list) and bool(ids)
        holds = []
        if isinstance(ids, list):
            for raw in ids:
                try:
                    key = UUID(str(raw))
                except ValueError:
                    complete = False
                    continue
                hold = self.db.get(CollectionHold, key, populate_existing=True)
                if (
                    hold is None
                    or hold.tenant_id != ctx.tenant_id
                    or hold.reason != REASON
                    or hold.evidence != marker
                ):
                    complete = False
                    continue
                holds.append(hold)
            complete = complete and len({h.id for h in holds}) == len(ids)
        pending = not complete or any(h.released_at is None for h in holds)
        allowed = True
        try:
            self._human(ctx, Permission.RESOLVE)
        except HTTPException as exc:
            if exc.status_code != 403:
                raise
            allowed = False
        return ContactStopView(
            action_id=action.id,
            action_version=action.version,
            customer_id=customer,
            accounting_connection_id=action.provider_connection_id,
            source_communication_id=source_id,
            voice_source=source_view(self.db, voice_id),
            source_excerpt=excerpt,
            original_holds=[
                ContactStopHoldView(
                    id=h.id,
                    invoice_id=h.invoice_id,
                    version=h.version,
                    released_at=h.released_at,
                    release_evidence=h.release_evidence,
                )
                for h in sorted(holds, key=lambda h: str(h.id))
            ],
            evidence_complete=complete,
            pending=pending,
            can_release=allowed and pending and complete,
        )

    def list(self, ctx: TenantContext, invoice_id: UUID) -> ContactStopPage:
        self._human(ctx, Permission.INVOICE_READ)
        invoice = self.db.get(InvoiceSnapshot, invoice_id, populate_existing=True)
        if invoice is None or invoice.tenant_id != ctx.tenant_id:
            raise HTTPException(404, "INVOICE_NOT_FOUND")
        rows = stop_actions(self.db, invoice)
        return ContactStopPage(items=[self._view(ctx, row) for row in rows])

    def release(self, ctx: TenantContext, action_id: UUID, command: ReleaseContactStop) -> ContactStopView:
        self._human(ctx, Permission.RESOLVE)
        action = self.db.scalar(
            select(ScheduledAction)
            .where(
                ScheduledAction.tenant_id == ctx.tenant_id,
                ScheduledAction.id == action_id,
                ScheduledAction.kind.in_(["agent_internal", "voice_internal"]),
                ScheduledAction.status == "succeeded",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if action is None:
            raise HTTPException(404, "CONTACT_STOP_NOT_FOUND")
        digest = hashlib.sha256(
            json.dumps(command.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        prior = self.db.scalar(
            select(AuditEvent).where(
                AuditEvent.tenant_id == ctx.tenant_id,
                AuditEvent.target_id == action.id,
                AuditEvent.action == "collection.contact_stop_released",
                AuditEvent.correlation_id == command.request_id,
            )
        )
        if prior is not None:
            if prior.actor_id != ctx.actor_id or prior.details.get("review_hash") != digest:
                raise HTTPException(409, "CONTACT_STOP_REVIEW_REQUEST_CONFLICT")
            return self._view(ctx, action)
        self.db.scalars(
            select(CollectionHold)
            .where(
                CollectionHold.tenant_id == ctx.tenant_id,
                CollectionHold.id.in_([h.hold_id for h in command.reviewed_holds]),
            )
            .order_by(CollectionHold.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        view = self._view(ctx, action)
        expected = sorted((str(h.id), h.version) for h in view.original_holds)
        captured = sorted((str(h.hold_id), h.expected_version) for h in command.reviewed_holds)
        if (
            action.version != command.expected_action_version
            or not view.can_release
            or captured != expected
            or len({h.hold_id for h in command.reviewed_holds}) != len(captured)
        ):
            raise HTTPException(409, "CONTACT_STOP_REVIEW_STALE")
        for hold in view.original_holds:
            release_hold(self.db, ctx, hold.id, command.reason)
        self.db.add(
            AuditEvent(
                tenant_id=ctx.tenant_id,
                actor_id=ctx.actor_id,
                correlation_id=command.request_id,
                action="collection.contact_stop_released",
                target_id=action.id,
                details={
                    "review_hash": digest,
                    "reviewed_action_version": command.expected_action_version,
                    "reviewed_holds": [h.model_dump(mode="json") for h in command.reviewed_holds],
                    "reason": command.reason,
                    "recipient_authority_changed": False,
                    "automatic_resume": False,
                },
            )
        )
        self.db.flush()
        return self._view(ctx, action)
