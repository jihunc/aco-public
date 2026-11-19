"""Guarded issue records; accounting remains authoritative and untouched."""

from __future__ import annotations

import builtins
import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.features.accounting.models import (
    CollectionRecord,
    Customer,
    InvoiceSnapshot,
    VerifiedPaymentAllocation,
)
from aco.features.cases.dto import (
    CaseHistoryView,
    CaseInvoiceView,
    CaseMutation,
    CasePage,
    CaseStatus,
    CaseView,
    CommunicationEvidence,
    DispositionReferences,
    EvidenceView,
    InvoiceIssueTransition,
    LinkCaseEvidence,
    TransitionCase,
    UpsertCase,
)
from aco.features.cases.identity import issue_identity, normalize
from aco.features.cases.models import CaseEvidence, CaseHistory, CaseInvoiceLink, CollectionCase
from aco.features.communications.models import Communication, CommunicationInvoiceLink, CommunicationThread
from aco.features.identity.models import now
from aco.features.identity.service import IdentityService, Permission
from aco.features.policies.models import CollectionHold
from aco.features.review.models import CollectionTask
from aco.features.review.queue import ReviewQueue
from aco.features.voice.evidence_dto import VoiceOperationalEvidence
from aco.features.voice.models import VoiceBusinessEvidence


def authorize_mutation(
    db: Session, ctx: Any, kind: str, command: BaseModel, permission: Permission = Permission.TASK_WRITE
) -> None:
    if isinstance(getattr(command, "evidence", None), VoiceOperationalEvidence):
        from aco.features.voice.tools import VoiceActionContext

        if not isinstance(ctx, VoiceActionContext):
            raise HTTPException(403, "VOICE_SOURCE_CREATION_REQUIRES_SIGNED_CALL")
    if isinstance(ctx, TenantContext):
        IdentityService(db).authorize(ctx, permission)
        return
    from aco.features.voice.tools import VoiceActionContext, require_voice_action

    if isinstance(ctx, VoiceActionContext):
        require_voice_action(db, ctx, command, kind=kind)
        return
    try:
        from aco.features.tools.authorization import AgentActionContext, require_agent_action
    except ImportError:
        raise HTTPException(403, "AGENT_AUTHORITY_UNAVAILABLE") from None
    if not isinstance(ctx, AgentActionContext):
        raise HTTPException(403, "INVALID_ACTOR_CONTEXT")
    require_agent_action(db, ctx, kind, command)


def require_human(db: Session, ctx: Any, permission: Permission = Permission.INVOICE_READ) -> None:
    if not isinstance(ctx, TenantContext):
        raise HTTPException(403, "HUMAN_REQUIRED")
    IdentityService(db).authorize(ctx, permission)


def scope_invoices(
    db: Session, tenant_id: UUID, customer_id: UUID, invoice_ids: list[UUID]
) -> list[InvoiceSnapshot]:
    customer = db.scalar(
        select(Customer)
        .where(Customer.tenant_id == tenant_id, Customer.id == customer_id)
        .execution_options(populate_existing=True)
    )
    if customer is None:
        raise HTTPException(403, "CUSTOMER_SCOPE_MISMATCH")
    if len(set(invoice_ids)) != len(invoice_ids):
        raise HTTPException(422, "DUPLICATE_INVOICE_SCOPE")
    rows = []
    for invoice_id in invoice_ids:
        row = db.scalar(
            select(InvoiceSnapshot)
            .where(
                InvoiceSnapshot.tenant_id == tenant_id,
                InvoiceSnapshot.id == invoice_id,
                InvoiceSnapshot.customer_id == customer_id,
            )
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise HTTPException(403, "INVOICE_SCOPE_MISMATCH")
        rows.append(row)
    return rows


def validate_evidence(
    db: Session,
    tenant_id: UUID,
    customer_id: UUID,
    evidence: CommunicationEvidence | VoiceOperationalEvidence,
    invoice_ids: list[UUID],
) -> Communication | VoiceBusinessEvidence:
    if isinstance(evidence, VoiceOperationalEvidence):
        from aco.features.voice.evidence import validate_reference

        return validate_reference(db, tenant_id, customer_id, evidence, invoice_ids)
    msg = db.scalar(
        select(Communication)
        .where(Communication.tenant_id == tenant_id, Communication.id == evidence.communication_id)
        .execution_options(populate_existing=True)
    )
    if msg is None or msg.direction != "incoming":
        raise HTTPException(403, "INVALID_COMMUNICATION_EVIDENCE")
    thread = db.scalar(
        select(CommunicationThread).where(
            CommunicationThread.tenant_id == tenant_id,
            CommunicationThread.id == msg.thread_id,
        )
    )
    if thread is None or (thread.customer_id is not None and thread.customer_id != customer_id):
        raise HTTPException(403, "EVIDENCE_CUSTOMER_MISMATCH")
    if thread.customer_id is None:
        # A human-reviewed exact original association does not retag its entire thread.
        reviewed = set(
            db.scalars(
                select(CommunicationInvoiceLink.invoice_id).where(
                    CommunicationInvoiceLink.tenant_id == tenant_id,
                    CommunicationInvoiceLink.communication_id == msg.id,
                    CommunicationInvoiceLink.classification == "reviewed_reference",
                )
            )
        )
        if not set(invoice_ids).issubset(reviewed):
            raise HTTPException(403, "EVIDENCE_CUSTOMER_MISMATCH")
        from aco.features.communications.association import candidates, retained_original

        retained_original(db, msg)
        scopes = candidates(db, msg)
        if not scopes or any(candidate.customer_id != customer_id for candidate in scopes):
            raise HTTPException(403, "EVIDENCE_CUSTOMER_MISMATCH")
    body = msg.content.get("text_body")
    if (
        not isinstance(body, str)
        or body[evidence.offset : evidence.offset + len(evidence.excerpt)] != evidence.excerpt
    ):
        raise HTTPException(422, "EVIDENCE_SPAN_MISMATCH")
    linked = set(
        db.scalars(
            select(CommunicationInvoiceLink.invoice_id).where(
                CommunicationInvoiceLink.tenant_id == tenant_id,
                CommunicationInvoiceLink.communication_id == msg.id,
            )
        )
    )
    if not set(invoice_ids).issubset(linked):
        raise HTTPException(422, "UNSUPPORTED_INVOICE_EVIDENCE")
    return msg


def place_hold(db: Session, ctx: Any, invoice_id: UUID, reason: str, evidence: str) -> UUID:
    collection = db.scalar(
        select(CollectionRecord)
        .where(CollectionRecord.tenant_id == ctx.tenant_id, CollectionRecord.invoice_id == invoice_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if collection is None:
        raise HTTPException(409, "COLLECTION_STATE_UNAVAILABLE")
    hold = CollectionHold(
        tenant_id=ctx.tenant_id,
        invoice_id=invoice_id,
        reason=reason,
        evidence=evidence,
        creator_id=ctx.actor_id,
        expires_at=None,
    )
    db.add(hold)
    collection.hold_reasons = sorted(set(collection.hold_reasons) | {reason})
    collection.version += 1
    db.flush()
    IdentityService(db).audit(ctx, "collection.hold_placed", hold.id)
    return hold.id


def release_hold(db: Session, ctx: Any, hold_id: UUID | None, evidence: str) -> None:
    if hold_id is None:
        return
    hold = db.scalar(
        select(CollectionHold)
        .where(CollectionHold.tenant_id == ctx.tenant_id, CollectionHold.id == hold_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if hold is None or hold.released_at is not None:
        return
    collection = db.scalar(
        select(CollectionRecord)
        .where(CollectionRecord.tenant_id == ctx.tenant_id, CollectionRecord.invoice_id == hold.invoice_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if collection is None:
        raise HTTPException(409, "COLLECTION_STATE_UNAVAILABLE")
    hold.released_at = now()
    hold.release_evidence = evidence
    hold.version += 1
    db.flush()
    remaining = set(
        db.scalars(
            select(CollectionHold.reason).where(
                CollectionHold.tenant_id == ctx.tenant_id,
                CollectionHold.invoice_id == hold.invoice_id,
                CollectionHold.released_at.is_(None),
            )
        )
    )
    collection.hold_reasons = sorted((set(collection.hold_reasons) - {hold.reason}) | remaining)
    collection.version += 1
    IdentityService(db).audit(ctx, "collection.hold_released", hold.id)


def create_review_task(
    db: Session,
    ctx: Any,
    title: str,
    notes: str,
    invoice_id: UUID | None = None,
    due_at: datetime | None = None,
) -> UUID:
    queue = ReviewQueue(db)
    state = queue.state(ctx.tenant_id)
    rank = max((r.priority_rank for r in queue.rows(ctx.tenant_id)), default=Decimal(0)) + 1
    row = CollectionTask(
        tenant_id=ctx.tenant_id,
        source_action_id=None,
        source_event_id=None,
        invoice_id=invoice_id,
        creator_id=ctx.actor_id,
        title=title,
        notes=notes,
        priority_rank=rank,
        due_at=due_at,
        command={},
    )
    db.add(row)
    state.version += 1
    db.flush()
    IdentityService(db).audit(ctx, "task.created_from_business_record", row.id)
    return row.id


def complete_review_task(db: Session, ctx: Any, task_id: UUID | None) -> None:
    """Close a business-record review task once its record was decided; idempotent."""
    if task_id is None:
        return
    row = db.get(CollectionTask, task_id, with_for_update=True)
    if row is None or row.tenant_id != ctx.tenant_id or row.status == "done":
        return
    row.status = "done"
    row.version += 1
    ReviewQueue(db).state(ctx.tenant_id).version += 1
    db.flush()
    IdentityService(db).audit(ctx, "task.completed_by_business_record", row.id)


class CaseService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _row(self, ctx: Any, case_id: UUID) -> CollectionCase:
        row = self.db.scalar(
            select(CollectionCase)
            .where(CollectionCase.tenant_id == ctx.tenant_id, CollectionCase.id == case_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise HTTPException(404, "CASE_NOT_FOUND")
        return row

    def _version(self, row: CollectionCase, expected: int) -> None:
        if row.version != expected:
            raise HTTPException(409, "STALE_CASE")

    def _links(self, row: CollectionCase) -> list[CaseInvoiceLink]:
        return list(
            self.db.scalars(
                select(CaseInvoiceLink)
                .where(CaseInvoiceLink.tenant_id == row.tenant_id, CaseInvoiceLink.case_id == row.id)
                .order_by(CaseInvoiceLink.created_at, CaseInvoiceLink.id)
                .execution_options(populate_existing=True)
            )
        )

    def _history(self, ctx: Any, row: CollectionCase, event: str, details: dict[str, object]) -> None:
        self.db.add(
            CaseHistory(
                tenant_id=ctx.tenant_id,
                case_id=row.id,
                actor_id=ctx.actor_id,
                event=event,
                substantive_human=isinstance(ctx, TenantContext),
                details=details,
            )
        )
        IdentityService(self.db).audit(ctx, event, row.id)

    def _evidence(
        self,
        ctx: Any,
        row: CollectionCase,
        evidence: CommunicationEvidence | VoiceOperationalEvidence,
        msg: Communication | VoiceBusinessEvidence,
        kind: str = "claim",
    ) -> tuple[CaseEvidence, bool]:
        evidence_key = hashlib.sha256(
            json.dumps(
                [str(msg.id), kind, evidence.offset, evidence.excerpt],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        existing = self.db.scalar(
            select(CaseEvidence).where(
                CaseEvidence.tenant_id == ctx.tenant_id,
                CaseEvidence.case_id == row.id,
                CaseEvidence.evidence_key == evidence_key,
            )
        )
        if existing:
            if (
                existing.communication_id or existing.voice_evidence_id,
                existing.kind,
                existing.offset,
                existing.excerpt,
            ) != (
                msg.id,
                kind,
                evidence.offset,
                evidence.excerpt,
            ):
                raise HTTPException(409, "EVIDENCE_IDENTITY_CONFLICT")
            return existing, False
        item = CaseEvidence(
            tenant_id=ctx.tenant_id,
            case_id=row.id,
            communication_id=msg.id if isinstance(msg, Communication) else None,
            voice_evidence_id=msg.id if isinstance(msg, VoiceBusinessEvidence) else None,
            kind=kind,
            evidence_key=evidence_key,
            excerpt=evidence.excerpt,
            offset=evidence.offset,
            synthetic=msg.synthetic,
            source_state=msg.source_state,
        )
        self.db.add(item)
        self.db.flush()
        return item, True

    def _view(self, row: CollectionCase) -> CaseView:
        from aco.features.voice.evidence import source_view

        self.db.flush()
        evidence = list(
            self.db.scalars(
                select(CaseEvidence)
                .where(CaseEvidence.tenant_id == row.tenant_id, CaseEvidence.case_id == row.id)
                .order_by(CaseEvidence.created_at, CaseEvidence.id)
            )
        )
        history = list(
            self.db.scalars(
                select(CaseHistory)
                .where(CaseHistory.tenant_id == row.tenant_id, CaseHistory.case_id == row.id)
                .order_by(CaseHistory.created_at, CaseHistory.id)
            )
        )
        return CaseView(
            id=row.id,
            customer_id=row.customer_id,
            issue_type=row.issue_type,
            claim_key=row.claim_key,
            substance=row.substance,
            status=cast(CaseStatus, row.status),
            version=row.version,
            opened_at=row.created_at,
            updated_at=row.updated_at,
            resolved_at=row.resolved_at,
            closed_at=row.closed_at,
            closure_reason=row.closure_reason,
            review_required=row.review_required,
            task_id=row.task_id,
            synthetic=row.synthetic,
            invoice_links=[
                CaseInvoiceView(
                    invoice_id=link.invoice_id,
                    issue_state=link.issue_state,
                    impact=link.impact,
                    hold_id=link.hold_id,
                    resolution_action=link.resolution_action,
                    resolution_evidence_id=link.resolution_evidence_id,
                    resolved_at=link.resolved_at,
                )
                for link in self._links(row)
            ],
            evidence=[
                EvidenceView(
                    id=e.id,
                    communication_id=e.communication_id,
                    voice_source=source_view(self.db, e.voice_evidence_id),
                    excerpt=e.excerpt,
                    offset=e.offset,
                    kind=e.kind,
                    synthetic=e.synthetic,
                    source_state=e.source_state,
                    created_at=e.created_at,
                )
                for e in evidence
            ],
            history=[
                CaseHistoryView(
                    id=h.id,
                    event=h.event,
                    actor_id=h.actor_id,
                    substantive_human=h.substantive_human,
                    details=h.details,
                    created_at=h.created_at,
                )
                for h in history
            ],
        )

    def get(self, ctx: Any, case_id: UUID) -> CaseView:
        require_human(self.db, ctx)
        return self._view(self._row(ctx, case_id))

    def list(self, ctx: Any, invoice_id: UUID | None = None, customer_id: UUID | None = None) -> CasePage:
        require_human(self.db, ctx)
        q = select(CollectionCase).where(CollectionCase.tenant_id == ctx.tenant_id)
        if customer_id:
            q = q.where(CollectionCase.customer_id == customer_id)
        if invoice_id:
            q = q.where(
                CollectionCase.id.in_(
                    select(CaseInvoiceLink.case_id).where(
                        CaseInvoiceLink.tenant_id == ctx.tenant_id, CaseInvoiceLink.invoice_id == invoice_id
                    )
                )
            )
        return CasePage(
            items=[
                self._view(r)
                for r in self.db.scalars(
                    q.order_by(CollectionCase.created_at.desc(), CollectionCase.id)
                    .limit(100)
                    .execution_options(populate_existing=True)
                )
            ]
        )

    def upsert_case(self, ctx: Any, command: UpsertCase) -> CaseMutation:
        authorize_mutation(self.db, ctx, "upsert_collection_case", command)
        scoped_invoices = scope_invoices(self.db, ctx.tenant_id, command.customer_id, command.invoice_ids)
        msg = validate_evidence(
            self.db, ctx.tenant_id, command.customer_id, command.evidence, command.invoice_ids
        )
        fingerprint = issue_identity(command.customer_id, command.issue_type, command.claim_key)
        row = self.db.scalar(
            select(CollectionCase)
            .where(CollectionCase.tenant_id == ctx.tenant_id, CollectionCase.issue_identity == fingerprint)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if command.matched_case_id:
            matched = self._row(ctx, command.matched_case_id)
            if matched.customer_id != command.customer_id:
                raise HTTPException(403, "CASE_CUSTOMER_MISMATCH")
            if command.matching == "same" and matched.issue_identity != fingerprint:
                raise HTTPException(409, "CASE_IDENTITY_CONFLICT")
            if command.matching == "ambiguous":
                row = matched
        elif command.matching == "same":
            raise HTTPException(422, "MATCHED_CASE_REQUIRED")
        created = row is None
        if row and command.expected_version is not None:
            self._version(row, command.expected_version)
        ambiguous = command.matching == "ambiguous" or (
            row is not None and normalize(row.substance) != normalize(command.substance)
        )
        if created:
            task_id = create_review_task(
                self.db, ctx, "Investigate collection issue", command.substance, command.invoice_ids[0]
            )
            row = CollectionCase(
                tenant_id=ctx.tenant_id,
                provider_connection_id=scoped_invoices[0].provider_connection_id,
                customer_id=command.customer_id,
                issue_identity=fingerprint,
                issue_type=command.issue_type,
                claim_key=command.claim_key,
                substance=command.substance,
                task_id=task_id,
                synthetic=msg.synthetic,
                review_required=ambiguous,
            )
            self.db.add(row)
            self.db.flush()
        assert row is not None
        evidence, added = self._evidence(
            ctx, row, command.evidence, msg, "ambiguous_match" if ambiguous else "claim"
        )
        links = self._links(row)
        linked = {link.invoice_id for link in links}
        changed = False
        if not ambiguous:
            for invoice_id in command.invoice_ids:
                if invoice_id not in linked:
                    hold = place_hold(
                        self.db, ctx, invoice_id, "active_dispute", f"Case {row.id}; evidence {evidence.id}"
                    )
                    self.db.add(
                        CaseInvoiceLink(
                            tenant_id=ctx.tenant_id,
                            case_id=row.id,
                            invoice_id=invoice_id,
                            impact="collection_hold",
                            hold_id=hold,
                        )
                    )
                    changed = True
        if ambiguous:
            row.review_required = True
        if added or changed or created:
            if not created:
                row.version += 1
            if row.status in {"RESOLVED", "CLOSED"} and added:
                self._reopen(ctx, row, evidence, command.invoice_ids)
            self._history(
                ctx,
                row,
                "case.created" if created else "case.evidence_added",
                {"evidence_id": str(evidence.id), "review_required": ambiguous},
            )
        return CaseMutation(case=self._view(row), created=created, review_required=row.review_required)

    def _reopen(
        self, ctx: Any, row: CollectionCase, evidence: CaseEvidence, invoice_ids: builtins.list[UUID]
    ) -> None:
        previous: dict[str, object] = {
            "previous_status": row.status,
            "previous_resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
            "previous_closed_at": row.closed_at.isoformat() if row.closed_at else None,
            "previous_closure_reason": row.closure_reason,
            "evidence_id": str(evidence.id),
        }
        row.status = "INVESTIGATING"
        row.resolved_at = None
        row.closed_at = None
        row.closure_reason = None
        for link in self._links(row):
            if link.invoice_id not in invoice_ids:
                continue
            link.issue_state = "OPEN"
            link.resolved_at = None
            link.resolution_action = None
            link.resolution_evidence_id = None
            link.version += 1
            # An existing unresolved hold remains valid; reopening does not clone it.
            hold = self.db.get(CollectionHold, link.hold_id) if link.hold_id else None
            if hold is None or hold.released_at is not None:
                link.hold_id = place_hold(
                    self.db,
                    ctx,
                    link.invoice_id,
                    "active_dispute",
                    f"Reopened case {row.id}; evidence {evidence.id}",
                )
        self._history(ctx, row, "case.reopened", previous)

    def _human_evidence(
        self,
        ctx: Any,
        row: CollectionCase,
        evidence: CommunicationEvidence | VoiceOperationalEvidence,
        invoice_ids: builtins.list[UUID],
    ) -> Communication | VoiceBusinessEvidence:
        source = validate_evidence(self.db, ctx.tenant_id, row.customer_id, evidence, invoice_ids)
        if isinstance(source, VoiceBusinessEvidence):
            # Human review reuses this case's exact retained signed origin. It
            # neither creates operational facts nor attributes another case's claim.
            retained = self.db.scalar(
                select(CaseEvidence.id).where(
                    CaseEvidence.tenant_id == ctx.tenant_id,
                    CaseEvidence.case_id == row.id,
                    CaseEvidence.voice_evidence_id == source.id,
                    CaseEvidence.communication_id.is_(None),
                )
            )
            if retained is None:
                raise HTTPException(403, "CASE_VOICE_ORIGIN_NOT_RETAINED")
            source_ids = source.facts.get("invoice_ids")
            if not isinstance(source_ids, list):
                raise HTTPException(403, "CASE_VOICE_SCOPE_DENIED")
            impacted = [link.invoice_id for link in self._links(row) if str(link.invoice_id) in source_ids]
            if not impacted:
                raise HTTPException(403, "CASE_VOICE_SCOPE_DENIED")
            validate_evidence(self.db, ctx.tenant_id, row.customer_id, evidence, impacted)
        return source

    def _source_invoice_ids(self, ctx: Any, source: Communication | VoiceBusinessEvidence) -> set[UUID]:
        if isinstance(source, VoiceBusinessEvidence):
            source_ids = source.facts.get("invoice_ids")
            assert isinstance(source_ids, list)
            return {UUID(str(value)) for value in source_ids}
        return set(
            self.db.scalars(
                select(CommunicationInvoiceLink.invoice_id).where(
                    CommunicationInvoiceLink.tenant_id == ctx.tenant_id,
                    CommunicationInvoiceLink.communication_id == source.id,
                )
            )
        )

    def link_case_evidence(self, ctx: Any, command: LinkCaseEvidence) -> EvidenceView:
        if isinstance(command.evidence, VoiceOperationalEvidence):
            require_human(self.db, ctx, Permission.TASK_WRITE)
        else:
            authorize_mutation(self.db, ctx, "link_case_evidence", command)
        row = self._row(ctx, command.case_id)
        self._version(row, command.expected_version)
        msg = self._human_evidence(ctx, row, command.evidence, [])
        evidence, added = self._evidence(ctx, row, command.evidence, msg)
        if added:
            if row.status in {"CLOSED", "RESOLVED"}:
                referenced = self._source_invoice_ids(ctx, msg)
                impacted = [link.invoice_id for link in self._links(row) if link.invoice_id in referenced]
                if not impacted:
                    raise HTTPException(422, "CASE_EVIDENCE_SCOPE_MISMATCH")
                self._reopen(ctx, row, evidence, impacted)
            row.version += 1
            self._history(ctx, row, "case.evidence_added", {"evidence_id": str(evidence.id)})
        return next(e for e in self._view(row).evidence if e.id == evidence.id)

    def transition_case(self, ctx: Any, command: TransitionCase) -> CaseView:
        require_human(
            self.db,
            ctx,
            Permission.RESOLVE if command.status in {"RESOLVED", "CLOSED"} else Permission.TASK_WRITE,
        )
        row = self._row(ctx, command.case_id)
        self._version(row, command.expected_version)
        transitions = {
            "OPEN": {"INVESTIGATING"},
            "INVESTIGATING": {"WAITING_CUSTOMER", "WAITING_INTERNAL", "AWAITING_APPROVAL", "RESOLVED"},
            "WAITING_CUSTOMER": {"INVESTIGATING"},
            "WAITING_INTERNAL": {"INVESTIGATING"},
            "AWAITING_APPROVAL": {"INVESTIGATING"},
            "RESOLVED": {"CLOSED"},
            "CLOSED": {"INVESTIGATING"},
        }
        if command.status == "RESOLVED" and (
            row.review_required or any(link.issue_state != "RESOLVED" for link in self._links(row))
        ):
            raise HTTPException(409, "UNRESOLVED_INVOICE_ISSUES")
        admin = command.status == "CLOSED" and command.closure_reason in {
            "duplicate_linked",
            "withdrawn",
            "out_of_scope",
            "invalid_claim",
        }
        if not admin and command.status not in transitions[row.status]:
            raise HTTPException(409, "INVALID_CASE_TRANSITION")
        if command.status == "CLOSED":
            if command.closure_reason is None:
                raise HTTPException(422, "CLOSURE_REASON_REQUIRED")
            if not admin and (row.status != "RESOLVED" or command.closure_reason != "resolved"):
                raise HTTPException(409, "UNRESOLVED_CASE")
            if admin:
                if command.evidence is None:
                    raise HTTPException(422, "CLOSURE_EVIDENCE_REQUIRED")
                self._human_evidence(ctx, row, command.evidence, [])
                if command.closure_reason == "duplicate_linked":
                    if command.surviving_case_id is None:
                        raise HTTPException(422, "SURVIVING_CASE_REQUIRED")
                    survivor = self._row(ctx, command.surviving_case_id)
                    if survivor.id == row.id or survivor.customer_id != row.customer_id:
                        raise HTTPException(409, "INVALID_SURVIVING_CASE")
            row.closed_at = now()
            row.closure_reason = command.closure_reason
        if row.status == "CLOSED" and command.status == "INVESTIGATING":
            if command.evidence is None:
                raise HTTPException(422, "NEW_EVIDENCE_REQUIRED")
            msg = self._human_evidence(ctx, row, command.evidence, [])
            evidence, added = self._evidence(ctx, row, command.evidence, msg)
            if not added:
                raise HTTPException(409, "NEW_EVIDENCE_REQUIRED")
            referenced = self._source_invoice_ids(ctx, msg)
            impacted = [link.invoice_id for link in self._links(row) if link.invoice_id in referenced]
            if not impacted:
                raise HTTPException(422, "CASE_EVIDENCE_SCOPE_MISMATCH")
            self._reopen(ctx, row, evidence, impacted)
        row.status = command.status
        row.version += 1
        if row.status == "RESOLVED":
            row.resolved_at = now()
        self._history(ctx, row, "case.transitioned", command.model_dump(mode="json"))
        return self._view(row)

    def disposition_references(self, ctx: Any, case_id: UUID, invoice_id: UUID) -> DispositionReferences:
        from aco.features.cases.dispositions import references

        require_human(self.db, ctx, Permission.RESOLVE)
        row = self._row(ctx, case_id)
        if invoice_id not in {link.invoice_id for link in self._links(row)}:
            raise HTTPException(403, "CASE_INVOICE_SCOPE_MISMATCH")
        invoice = scope_invoices(self.db, ctx.tenant_id, row.customer_id, [invoice_id])[0]
        items = (
            references(self.db, invoice, row.issue_type, ctx.actor_id)
            if row.status == "INVESTIGATING"
            else []
        )
        return DispositionReferences(
            case_id=row.id,
            case_version=row.version,
            invoice_id=invoice_id,
            items=items,
            unavailable_reason=None if items else "NO_CURRENT_VERIFIED_DISPOSITION",
        )

    def set_invoice_issue_state(self, ctx: Any, command: InvoiceIssueTransition) -> CaseView:
        require_human(self.db, ctx, Permission.RESOLVE)
        row = self._row(ctx, command.case_id)
        self._version(row, command.expected_version)
        if row.status != "INVESTIGATING":
            raise HTTPException(409, "CASE_NOT_INVESTIGATING")
        link = next((link for link in self._links(row) if link.invoice_id == command.invoice_id), None)
        if link is None:
            raise HTTPException(403, "CASE_INVOICE_SCOPE_MISMATCH")
        msg = self._human_evidence(ctx, row, command.evidence, [command.invoice_id])
        if isinstance(msg, VoiceBusinessEvidence) and row.issue_type not in {
            "refund",
            "payment_not_found",
            "contact_correction",
            "stop_contact",
        }:
            raise HTTPException(422, "DOCUMENTARY_DISPOSITION_EVIDENCE_REQUIRED")
        disposition_details: dict[str, object] = {}
        if (
            row.issue_type in {"wrong_amount", "wrong_po"}
            and command.resolution_kind != "documentary_confirmation"
        ):
            raise HTTPException(422, "ISSUE_DISPOSITION_EVIDENCE_REQUIRED")
        if row.issue_type == "payment_not_found":
            payment = self.db.scalar(
                select(VerifiedPaymentAllocation).where(
                    VerifiedPaymentAllocation.tenant_id == ctx.tenant_id,
                    VerifiedPaymentAllocation.invoice_id == command.invoice_id,
                    VerifiedPaymentAllocation.id == command.resolution_reference_id,
                )
            )
            invoice = scope_invoices(self.db, ctx.tenant_id, row.customer_id, [command.invoice_id])[0]
            if (
                command.resolution_kind != "payment_allocation"
                or payment is None
                or payment.kind != "cash"
                or payment.minor_units <= 0
                or (payment.currency, payment.exponent) != (invoice.currency, invoice.exponent)
            ):
                raise HTTPException(422, "VERIFIED_PAYMENT_EVIDENCE_REQUIRED")
            adjustment = self.db.scalar(
                select(VerifiedPaymentAllocation.id)
                .where(
                    VerifiedPaymentAllocation.tenant_id == ctx.tenant_id,
                    VerifiedPaymentAllocation.provider_connection_id == payment.provider_connection_id,
                    VerifiedPaymentAllocation.parent_id == payment.id,
                    VerifiedPaymentAllocation.kind.in_(["refund", "reversal"]),
                )
                .limit(1)
            )
            if adjustment is not None:
                raise HTTPException(422, "PAYMENT_ADJUSTMENT_REQUIRES_REVIEW")
        elif row.issue_type in {"refund", "stop_contact", "contact_correction"}:
            from aco.features.cases.dispositions import verify_reference

            invoice = scope_invoices(self.db, ctx.tenant_id, row.customer_id, [command.invoice_id])[0]
            reference = verify_reference(
                self.db,
                invoice,
                row.issue_type,
                resolver_id=ctx.actor_id,
                reference_id=command.resolution_reference_id,
                kind=command.resolution_kind,
                version=command.expected_reference_version,
                captured_hash=command.expected_reference_hash,
            )
            disposition_details["disposition_reference"] = reference.model_dump(mode="json")
        elif row.issue_type not in {"wrong_amount", "wrong_po"}:
            raise HTTPException(409, "ISSUE_REQUIRES_SPECIALIST_REVIEW")
        evidence, _ = self._evidence(ctx, row, command.evidence, msg, "resolution")
        link.issue_state = "RESOLVED"
        link.resolution_action = command.resolution_action
        link.resolution_evidence_id = evidence.id
        link.resolved_at = now()
        link.version += 1
        release_hold(self.db, ctx, link.hold_id, f"Resolved issue with case evidence {evidence.id}")
        row.version += 1
        self._history(
            ctx,
            row,
            "case.invoice_issue_resolved",
            {**command.model_dump(mode="json"), **disposition_details},
        )
        return self._view(row)
