"""Current-human reviewed assumptions and transparent recorded operating evidence."""

import hashlib
import json
from decimal import Decimal, localcontext
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.features.cases.service import require_human
from aco.features.identity.models import Tenant, now
from aco.features.identity.service import IdentityService, Permission
from aco.features.impact.case_outcome_service import CaseOutcomeService
from aco.features.impact.cost_allocation import CostAllocationService
from aco.features.impact.economics import cost_components, estimate_capacity
from aco.features.impact.economics_dto import (
    AllocatedCostRollup,
    ApproveEconomicsAssumptions,
    EconomicsAssumptionView,
    EconomicsGroup,
    EconomicsQuery,
    EconomicsView,
    SaveEconomicsAssumptions,
    UsageCostEvidence,
)
from aco.features.impact.models import EconomicsApproval, EconomicsAssumptions
from aco.features.usage.dto import UsageFilters
from aco.features.usage.models import ProviderUsageEvent, UsageAttempt, UsageRevision
from aco.features.usage.service import UsageService


def allocated_rollups(events: list[UsageCostEvidence], *, by_case: bool) -> list[AllocatedCostRollup]:
    values: dict[tuple[UUID | None, str | None, bool], Decimal] = {}
    identities: dict[tuple[UUID | None, str | None, bool], set[UUID]] = {}
    unknown: dict[tuple[UUID | None, str | None, bool], set[UUID]] = {}
    with localcontext() as precision:
        precision.prec = 80
        for event in events:
            for leaf in event.allocation.leaves:
                key = (leaf.case_id if by_case else leaf.invoice_id, event.currency, event.synthetic)
                values.setdefault(key, Decimal(0))
                identities.setdefault(key, set()).add(event.event_id)
                unknown.setdefault(key, set())
                if event.amount is None:
                    unknown[key].add(event.event_id)
                elif event.additive:
                    values[key] += event.amount * leaf.weight
    return [
        AllocatedCostRollup(
            target_id=key[0],
            currency=key[1],
            synthetic=key[2],
            known_amount=values[key],
            unknown_event_count=len(unknown[key]),
            event_ids=sorted(identities[key], key=str),
        )
        for key in sorted(values, key=lambda item: (str(item[0]), str(item[1]), item[2]))
    ]


class EconomicsService:
    def __init__(self, db: Session):
        self.db = db

    def owner(self, ctx: TenantContext) -> None:
        require_human(self.db, ctx, Permission.SETTINGS)
        self.db.scalar(select(Tenant).where(Tenant.id == ctx.tenant_id).with_for_update(key_share=True))

    def latest(self, ctx: TenantContext) -> EconomicsAssumptions | None:
        return self.db.scalar(
            select(EconomicsAssumptions)
            .where(EconomicsAssumptions.tenant_id == ctx.tenant_id)
            .order_by(EconomicsAssumptions.version.desc())
            .limit(1)
        )

    def latest_approval(self, ctx: TenantContext) -> EconomicsApproval | None:
        return self.db.scalar(
            select(EconomicsApproval)
            .where(EconomicsApproval.tenant_id == ctx.tenant_id)
            .order_by(EconomicsApproval.version.desc())
            .limit(1)
        )

    def view(self, row: EconomicsAssumptions) -> EconomicsAssumptionView:
        approval = self.db.scalar(
            select(EconomicsApproval).where(
                EconomicsApproval.tenant_id == row.tenant_id, EconomicsApproval.assumptions_id == row.id
            )
        )
        return EconomicsAssumptionView(
            id=row.id,
            version=row.version,
            previous_version_id=row.previous_version_id,
            creator_id=row.actor_id,
            created_at=row.created_at,
            reviewed_hash=row.reviewed_hash,
            command=SaveEconomicsAssumptions.model_validate(row.command),
            approval_id=approval.id if approval else None,
            approved_by=approval.actor_id if approval else None,
            approved_at=approval.created_at if approval else None,
            approval_evidence=str(approval.command["evidence"]) if approval else None,
        )

    def save(self, ctx: TenantContext, command: SaveEconomicsAssumptions) -> EconomicsAssumptionView:
        self.owner(ctx)
        payload = command.model_dump(mode="json")
        previous_request = self.db.scalar(
            select(EconomicsAssumptions).where(
                EconomicsAssumptions.tenant_id == ctx.tenant_id,
                EconomicsAssumptions.request_id == command.request_id,
            )
        )
        if previous_request is not None:
            if previous_request.command != payload or previous_request.actor_id != ctx.actor_id:
                raise HTTPException(409, "ECONOMICS_ASSUMPTIONS_REQUEST_CONFLICT")
            return self.view(previous_request)
        latest = self.latest(ctx)
        if command.expected_previous_version_id != (latest.id if latest else None):
            raise HTTPException(409, "ECONOMICS_ASSUMPTIONS_CHANGED")
        key = uuid4()
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        audit = IdentityService(self.db).audit(
            ctx,
            "economics.assumptions_proposed",
            key,
            {"reviewed_hash": digest, "synthetic": command.synthetic},
        )
        row = EconomicsAssumptions(
            id=key,
            tenant_id=ctx.tenant_id,
            request_id=command.request_id,
            actor_id=ctx.actor_id,
            audit_id=audit.id,
            previous_version_id=latest.id if latest else None,
            version=latest.version + 1 if latest else 1,
            command=payload,
            reviewed_hash=digest,
        )
        self.db.add(row)
        self.db.flush()
        return self.view(row)

    def approve(
        self, ctx: TenantContext, assumptions_id: UUID, command: ApproveEconomicsAssumptions
    ) -> EconomicsAssumptionView:
        self.owner(ctx)
        row = self.db.scalar(
            select(EconomicsAssumptions).where(
                EconomicsAssumptions.tenant_id == ctx.tenant_id, EconomicsAssumptions.id == assumptions_id
            )
        )
        if row is None:
            raise HTTPException(404, "ECONOMICS_ASSUMPTIONS_NOT_FOUND")
        payload = {"assumptions_id": str(assumptions_id), **command.model_dump(mode="json")}
        replay = self.db.scalar(
            select(EconomicsApproval).where(
                EconomicsApproval.tenant_id == ctx.tenant_id,
                EconomicsApproval.request_id == command.request_id,
            )
        )
        if replay is not None:
            if replay.command != payload or replay.actor_id != ctx.actor_id:
                raise HTTPException(409, "ECONOMICS_APPROVAL_REQUEST_CONFLICT")
            return self.view(row)
        current = self.latest_approval(ctx)
        latest = self.latest(ctx)
        if (
            command.reviewed_hash != row.reviewed_hash
            or command.expected_current_approval_id != (current.id if current else None)
            or latest is None
            or latest.id != row.id
        ):
            raise HTTPException(409, "ECONOMICS_REVIEW_CHANGED")
        if self.db.scalar(
            select(EconomicsApproval).where(
                EconomicsApproval.tenant_id == ctx.tenant_id, EconomicsApproval.assumptions_id == row.id
            )
        ):
            raise HTTPException(409, "ECONOMICS_ALREADY_APPROVED")
        key = uuid4()
        audit = IdentityService(self.db).audit(
            ctx,
            "economics.assumptions_approved",
            key,
            {"assumptions_id": str(row.id), "reviewed_hash": row.reviewed_hash},
        )
        approval = EconomicsApproval(
            id=key,
            tenant_id=ctx.tenant_id,
            request_id=command.request_id,
            actor_id=ctx.actor_id,
            audit_id=audit.id,
            assumptions_id=row.id,
            previous_approval_id=current.id if current else None,
            version=current.version + 1 if current else 1,
            command=payload,
        )
        self.db.add(approval)
        self.db.flush()
        return self.view(row)

    def read(self, ctx: TenantContext, query: EconomicsQuery) -> EconomicsView:
        require_human(self.db, ctx)
        as_of = now()
        if query.since > as_of:
            raise HTTPException(422, "ECONOMICS_WINDOW_IN_FUTURE")
        can_manage = True
        try:
            require_human(self.db, ctx, Permission.SETTINGS)
        except HTTPException as error:
            if error.status_code != 403:
                raise
            can_manage = False
        rows = list(
            self.db.scalars(
                select(EconomicsAssumptions)
                .where(EconomicsAssumptions.tenant_id == ctx.tenant_id)
                .order_by(EconomicsAssumptions.version)
            )
        )
        if len(rows) > 1000:
            raise HTTPException(422, "ECONOMICS_HISTORY_TOO_LARGE")
        assumptions = [self.view(row) for row in rows]
        approved = {}
        for view in assumptions:
            c = view.command
            if view.approval_id is not None and c.since == query.since and c.until == query.until:
                approved[(c.currency, c.synthetic)] = view
        usage = UsageService(self.db).list_usage(ctx, UsageFilters(since=query.since, until=query.until))
        cases = CaseOutcomeService(self.db).read(ctx, query.since, query.until, as_of)
        event_rows = list(
            self.db.scalars(
                select(ProviderUsageEvent)
                .where(
                    ProviderUsageEvent.tenant_id == ctx.tenant_id,
                    ProviderUsageEvent.created_at >= query.since,
                    ProviderUsageEvent.created_at < query.until,
                )
                .order_by(ProviderUsageEvent.created_at, ProviderUsageEvent.id)
                .limit(5001)
            )
        )
        if len(event_rows) > 5000:
            raise HTTPException(422, "ECONOMICS_USAGE_WINDOW_TOO_LARGE")
        allocation = CostAllocationService(self.db)
        events = []
        for event in event_rows:
            revision = self.db.scalar(
                select(UsageRevision)
                .where(UsageRevision.event_id == event.id)
                .order_by(UsageRevision.version.desc())
                .limit(1)
            )
            events.append(
                UsageCostEvidence(
                    event_id=event.id,
                    revision_id=revision.id if revision else None,
                    revision_version=revision.version if revision else None,
                    provider_connection_id=event.provider_connection_id,
                    operation_key=event.operation_key,
                    component=event.component,
                    provider=event.provider,
                    currency=revision.currency if revision else None,
                    synthetic=event.synthetic,
                    amount=revision.cost_amount if revision else None,
                    quality=revision.cost_quality if revision else "UNKNOWN",
                    additive=not bool(revision.payload.get("included_in")) if revision else False,
                    created_at=event.created_at,
                    allocation=allocation.view(event, allocation.latest(event)),
                )
            )
        missing_attempts = bool(
            self.db.scalar(
                select(UsageAttempt.id)
                .where(
                    UsageAttempt.tenant_id == ctx.tenant_id,
                    UsageAttempt.created_at >= query.since,
                    UsageAttempt.created_at < query.until,
                    UsageAttempt.event_id.is_(None),
                )
                .limit(1)
            )
        )
        keys = set(approved) | {(item.currency, item.synthetic) for item in usage.currencies}
        groups = []
        for currency, synthetic in sorted(keys):
            basis = next(
                (
                    item
                    for item in usage.currencies
                    if (item.currency, item.synthetic) == (currency, synthetic)
                ),
                None,
            )
            selected_events = [
                event for event in events if event.currency == currency and event.synthetic == synthetic
            ]
            known = Decimal(basis.known_amount) if basis is not None and selected_events else None
            complete = (
                bool(selected_events)
                and basis is not None
                and basis.completeness == "complete"
                and not missing_attempts
                and all(
                    event.revision_id is not None and event.allocation.current_source_matches
                    for event in selected_events
                )
            )
            foreign_currency = any(
                event.synthetic == synthetic and event.currency != currency for event in events
            )
            selected_cases = [case for case in cases.cases if case.synthetic == synthetic]
            human = (
                sum(
                    (
                        case.reported_human_minutes
                        for case in selected_cases
                        if case.reported_human_minutes is not None
                    ),
                    Decimal(0),
                )
                if selected_cases and all(case.reported_human_minutes is not None for case in selected_cases)
                else None
            )
            assumption = approved.get((currency, synthetic))
            command = assumption.command if assumption else None
            wage = command.wage_per_hour if command else None
            fixed = command.fixed_cost if command else None
            reasons = []
            if not complete:
                reasons.append(
                    "Provider billing coverage is incomplete; known recorded amounts remain visible."
                )
            if foreign_currency:
                reasons.append("Other provider currencies exist; no FX conversion or combined ROI.")
            if human is None:
                reasons.append("Complete reported human time is unavailable; no inferred zero.")
            if command is None:
                reasons.append("No Owner-approved assumptions for this exact window/currency/source basis.")
            if fixed is None:
                reasons.append("No explicit fixed-cost allocation, including an evidenced zero.")
            reasons.append(
                "Measured labour is unavailable; reported labour and capacity are estimates, "
                "and canonical ROI remains Insufficient data."
            )
            estimated_costs = cost_components(
                provider=known,
                human_minutes=human,
                wage=wage,
                fixed=fixed,
                resolved=sum(case.eligible_resolved for case in selected_cases),
            )
            groups.append(
                EconomicsGroup(
                    currency=currency,
                    synthetic=synthetic,
                    eligible_case_count=len(selected_cases),
                    resolved_case_count=sum(case.eligible_resolved for case in selected_cases),
                    case_ids=[case.case_id for case in selected_cases],
                    handling_entry_ids=[key for case in selected_cases for key in case.handling_entry_ids],
                    human_time_basis="MANUAL_REPORTED" if human is not None else "UNAVAILABLE",
                    reported_human_minutes=human,
                    estimated_reported_human_cost=estimated_costs.human,
                    estimated_cost_per_resolution=estimated_costs.complete_cost_per_resolution
                    if complete and not foreign_currency
                    else None,
                    known_provider_cost=known,
                    provider_basis_complete=complete,
                    assumptions_id=assumption.id if assumption else None,
                    costs=cost_components(
                        provider=known if complete else None,
                        human_minutes=None,
                        wage=wage,
                        fixed=fixed,
                        resolved=sum(case.eligible_resolved for case in selected_cases),
                    ),
                    capacity=estimate_capacity(
                        eligible=len(selected_cases),
                        baseline=command.baseline_minutes_per_issue if command else None,
                        human_minutes=human,
                        wage=wage,
                        provider=known if complete else None,
                        fixed=fixed,
                        approved=command is not None,
                        same_currency=not foreign_currency,
                        human_time_measured=False,
                    ),
                    reasons=reasons,
                )
            )
        current = self.latest_approval(ctx)
        return EconomicsView(
            tenant_id=ctx.tenant_id,
            since=query.since,
            until=query.until,
            as_of_at=as_of,
            can_manage=can_manage,
            current_approval_id=current.id if current else None,
            latest_assumption_id=rows[-1].id if rows else None,
            assumptions=assumptions,
            groups=groups,
            usage=usage,
            events=events,
            case_costs=allocated_rollups(events, by_case=True),
            invoice_costs=allocated_rollups(events, by_case=False),
        )
