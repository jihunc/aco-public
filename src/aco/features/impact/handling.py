"""Current-human reporting of their own work; corrections preserve every original."""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.features.cases.models import CaseHistory, CollectionCase
from aco.features.cases.service import require_human
from aco.features.identity.service import IdentityService, Permission
from aco.features.impact.handling_dto import HandlingTimePage, HandlingTimeView, RecordHandlingTime
from aco.features.impact.models import HumanHandlingEntry


def view(row: HumanHandlingEntry) -> HandlingTimeView:
    return HandlingTimeView(
        id=row.id,
        case_id=row.case_id,
        history_id=row.history_id,
        actor_id=row.actor_id,
        minutes=row.minutes,
        evidence=row.evidence,
        supersedes_id=row.supersedes_id,
        version=row.version,
        created_at=row.created_at,
        audit_id=row.audit_id,
    )


class HandlingTimeService:
    def __init__(self, db: Session):
        self.db = db

    def case(self, ctx: TenantContext, case_id: UUID) -> CollectionCase:
        row = self.db.scalar(
            select(CollectionCase)
            .where(CollectionCase.tenant_id == ctx.tenant_id, CollectionCase.id == case_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise HTTPException(404, "CASE_NOT_FOUND")
        return row

    def read(self, ctx: TenantContext, case_id: UUID) -> HandlingTimePage:
        require_human(self.db, ctx)
        row = self.case(ctx, case_id)
        entries = list(
            self.db.scalars(
                select(HumanHandlingEntry)
                .where(HumanHandlingEntry.tenant_id == ctx.tenant_id, HumanHandlingEntry.case_id == case_id)
                .order_by(HumanHandlingEntry.created_at, HumanHandlingEntry.id)
            )
        )
        latest = {e.history_id: e for e in entries}
        permitted = []
        try:
            require_human(self.db, ctx, Permission.TASK_WRITE)
            permitted = list(
                self.db.scalars(
                    select(CaseHistory.id)
                    .where(
                        CaseHistory.tenant_id == ctx.tenant_id,
                        CaseHistory.case_id == case_id,
                        CaseHistory.actor_id == ctx.actor_id,
                        CaseHistory.substantive_human.is_(True),
                    )
                    .order_by(CaseHistory.created_at, CaseHistory.id)
                )
            )
        except HTTPException as error:
            if error.status_code != 403:
                raise
        return HandlingTimePage(
            case_id=case_id,
            expected_case_version=row.version,
            can_record_history_ids=permitted,
            entries=[view(e) for e in entries],
            current_entry_ids=[e.id for e in latest.values()],
        )

    def record(self, ctx: TenantContext, case_id: UUID, command: RecordHandlingTime) -> HandlingTimeView:
        require_human(self.db, ctx, Permission.TASK_WRITE)
        row = self.case(ctx, case_id)
        history = self.db.scalar(
            select(CaseHistory).where(
                CaseHistory.tenant_id == ctx.tenant_id,
                CaseHistory.case_id == case_id,
                CaseHistory.id == command.history_id,
                CaseHistory.actor_id == ctx.actor_id,
                CaseHistory.substantive_human.is_(True),
            )
        )
        if history is None:
            raise HTTPException(403, "HANDLING_EVENT_NOT_OWN_SUBSTANTIVE_WORK")
        payload = {"case_id": str(case_id), **command.model_dump(mode="json")}
        existing = self.db.scalar(
            select(HumanHandlingEntry).where(
                HumanHandlingEntry.tenant_id == ctx.tenant_id,
                HumanHandlingEntry.request_id == command.request_id,
            )
        )
        if existing is not None:
            if existing.command != payload or existing.actor_id != ctx.actor_id:
                raise HTTPException(409, "HANDLING_REQUEST_CONFLICT")
            return view(existing)
        if row.version != command.expected_case_version:
            raise HTTPException(409, "CASE_VERSION_CONFLICT")
        latest = self.db.scalar(
            select(HumanHandlingEntry)
            .where(HumanHandlingEntry.tenant_id == ctx.tenant_id, HumanHandlingEntry.history_id == history.id)
            .order_by(HumanHandlingEntry.version.desc())
            .limit(1)
        )
        if command.supersedes_id != (latest.id if latest else None):
            raise HTTPException(409, "HANDLING_REPORT_CHANGED")
        audit = IdentityService(self.db).audit(
            ctx,
            "case.handling_time_reported",
            case_id,
            {"history_id": str(history.id), "basis": "MANUAL_REPORTED", "minutes": str(command.minutes)},
        )
        entry = HumanHandlingEntry(
            tenant_id=ctx.tenant_id,
            case_id=case_id,
            history_id=history.id,
            actor_id=ctx.actor_id,
            request_id=command.request_id,
            command=payload,
            minutes=command.minutes,
            evidence=command.evidence,
            supersedes_id=command.supersedes_id,
            version=latest.version + 1 if latest else 1,
            audit_id=audit.id,
        )
        self.db.add(entry)
        self.db.flush()
        return view(entry)
