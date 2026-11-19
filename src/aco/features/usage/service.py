"""Canonical charge recording and tenant summaries, independent of invoice joins."""

from datetime import datetime
from decimal import Decimal, localcontext
from typing import Protocol, cast
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from aco.core.context import TenantContext
from aco.features.identity.models import ProviderConnection
from aco.features.identity.service import IdentityService, Permission
from aco.features.usage.dto import (
    BundleIssue,
    BundleIssueReason,
    CurrencyUsage,
    Dimension,
    LimitView,
    ProviderUsageBasis,
    ProviderUsageInput,
    UsageFilters,
    UsageSummary,
)
from aco.features.usage.models import (
    BudgetLimit,
    BudgetReservation,
    BudgetScope,
    ProviderUsageEvent,
    UsageAttempt,
    UsageRevision,
)


class UsageContext(Protocol):
    """Attribution only; producers must independently authorize their own operations."""

    @property
    def tenant_id(self) -> UUID: ...
    @property
    def actor_id(self) -> UUID: ...
    @property
    def correlation_id(self) -> UUID: ...


def connection_for(db: Session, ctx: UsageContext, connection_id: UUID) -> ProviderConnection:
    connection = db.scalar(
        select(ProviderConnection)
        .where(ProviderConnection.id == connection_id, ProviderConnection.tenant_id == ctx.tenant_id)
        .execution_options(populate_existing=True)
    )
    if connection is None:
        raise HTTPException(403, "FOREIGN_CONNECTION")
    return connection


class UsageService:
    def __init__(self, db: Session):
        self.db = db

    def record_usage(self, ctx: TenantContext, event: ProviderUsageInput) -> ProviderUsageEvent:
        IdentityService(self.db).authorize(ctx, Permission.SETTINGS)
        connection = connection_for(self.db, ctx, event.provider_connection_id)
        if (connection.provider, connection.provider_type) != (event.provider, event.provider_kind):
            raise HTTPException(422, "PROVIDER_IDENTITY_CONFLICT")
        return self.record_verified(ctx, event)

    def record_verified(self, ctx: UsageContext, event: ProviderUsageInput) -> ProviderUsageEvent:
        """Internal producer seam; caller already verified committed scope, not an HTTP API."""
        if event.included_in:
            parent = self.db.scalar(
                select(ProviderUsageEvent).where(
                    ProviderUsageEvent.scope_key == str(ctx.tenant_id),
                    ProviderUsageEvent.provider_connection_id == event.provider_connection_id,
                    ProviderUsageEvent.operation_key == event.operation_key,
                    ProviderUsageEvent.component == event.included_in,
                )
            )
            parent_revision = (
                self.db.scalar(
                    select(UsageRevision)
                    .where(UsageRevision.event_id == parent.id)
                    .order_by(UsageRevision.version.desc())
                    .limit(1)
                )
                if parent
                else None
            )
            if (
                parent is None
                or parent_revision is None
                or not event.bundle
                or parent_revision.payload.get("bundle") != event.bundle
                or parent_revision.payload.get("included_in")
                or parent_revision.currency != event.cost.currency
                or parent_revision.cost_amount is None
                or parent.synthetic != event.synthetic
                or (event.cost.amount is not None and event.cost.amount > parent_revision.cost_amount)
            ):
                raise HTTPException(422, "BUNDLE_PARENT_UNVERIFIED")
        if event.cost.quality.value == "RATE_ESTIMATE" and event.cost.rate_snapshot_id is None:
            raise HTTPException(422, "RATE_SNAPSHOT_REQUIRED")
        if event.cost.rate_snapshot_id is not None:
            from aco.features.usage.models import RateSnapshot

            rate = self.db.get(RateSnapshot, event.cost.rate_snapshot_id)
            if (
                rate is None
                or rate.tenant_id != ctx.tenant_id
                or rate.currency != event.cost.currency
                or rate.provider != event.provider
                or rate.synthetic != event.synthetic
                or rate.price_source_at != event.price_source_at
            ):
                raise HTTPException(422, "RATE_SCOPE_CONFLICT")
            if event.cost.quality.value == "RATE_ESTIMATE":
                counters = event.units.model_dump()
                if rate.unit not in counters:
                    raise HTTPException(422, "RATE_UNIT_UNSUPPORTED")
                quantity = Decimal(str(counters[rate.unit]))
                if rate.unit == "input_tokens":
                    quantity -= event.units.cached_tokens
                with localcontext() as precision:
                    precision.prec = 80
                    if event.cost.amount != quantity * rate.amount:
                        raise HTTPException(422, "RATE_ESTIMATE_MISMATCH")
        row = self.db.scalar(
            select(ProviderUsageEvent)
            .where(
                ProviderUsageEvent.scope_key == str(ctx.tenant_id),
                ProviderUsageEvent.provider_connection_id == event.provider_connection_id,
                ProviderUsageEvent.operation_key == event.operation_key,
                ProviderUsageEvent.component == event.component,
            )
            .with_for_update()
        )
        if row is None:
            row = ProviderUsageEvent(
                tenant_id=ctx.tenant_id,
                scope_kind="TENANT",
                scope_key=str(ctx.tenant_id),
                provider_connection_id=event.provider_connection_id,
                operation_key=event.operation_key,
                component=event.component,
                operation=event.operation,
                provider=event.provider,
                provider_kind=event.provider_kind,
                correlation_id=ctx.correlation_id,
                native_operation_id=event.native_operation_id,
                native_subject=event.native_subject,
                synthetic=event.synthetic,
            )
            self.db.add(row)
            self.db.flush()
        elif (row.operation, row.provider, row.synthetic, row.native_subject) != (
            event.operation,
            event.provider,
            event.synthetic,
            event.native_subject,
        ):
            raise HTTPException(409, "USAGE_IDENTITY_CONFLICT")
        payload = event.model_dump(mode="json")
        if event.native_subject is None:
            # Preserve exact replay of historical revisions before this additive field.
            payload.pop("native_subject")
        previous = self.db.scalar(
            select(UsageRevision).where(
                UsageRevision.event_id == row.id, UsageRevision.reconciliation_key == event.reconciliation_key
            )
        )
        if previous:
            if previous.payload != payload:
                raise HTTPException(409, "USAGE_REPLAY_CONFLICT")
            return row
        latest = self.db.scalar(
            select(UsageRevision)
            .where(UsageRevision.event_id == row.id)
            .order_by(UsageRevision.version.desc())
            .limit(1)
        )
        if latest and latest.currency != event.cost.currency:
            raise HTTPException(409, "USAGE_CURRENCY_CONFLICT")
        self.db.add(
            UsageRevision(
                event_id=row.id,
                reconciliation_key=event.reconciliation_key,
                cost_amount=event.cost.amount,
                currency=event.cost.currency,
                cost_quality=event.cost.quality.value,
                outcome=event.outcome,
                payload=payload,
                version=latest.version + 1 if latest else 1,
            )
        )
        self.db.flush()
        return row

    def list_usage(self, ctx: TenantContext, filters: UsageFilters | None = None) -> UsageSummary:
        if not isinstance(ctx, TenantContext):
            raise HTTPException(403, "HUMAN_CONTEXT_REQUIRED")
        IdentityService(self.db).authorize(ctx, Permission.INVOICE_READ)
        filters = filters or UsageFilters()
        if filters.provider_connection_id:
            connection_for(self.db, ctx, filters.provider_connection_id)

        def included(connection_id: UUID, created_at: datetime) -> bool:
            return (
                (filters.provider_connection_id is None or filters.provider_connection_id == connection_id)
                and (filters.since is None or created_at >= filters.since)
                and (filters.until is None or created_at < filters.until)
            )

        bundle_issues: list[BundleIssue] = []
        providers: dict[tuple[UUID, str, bool], ProviderUsageBasis] = {}
        groups: dict[tuple[str, bool], dict[str, Decimal | int]] = {}
        notices: set[str] = {"Real external execution is BLOCKED. Draft limits do not authorize spending."}

        def group(currency: str, synthetic: bool) -> dict[str, Decimal | int]:
            return groups.setdefault(
                (currency, synthetic),
                {
                    "known": Decimal(0),
                    "reported": Decimal(0),
                    "estimated": Decimal(0),
                    "unknown": 0,
                    "issues": 0,
                    "reserved": Decimal(0),
                    "spent": Decimal(0),
                },
            )

        with localcontext() as precision:
            precision.prec = 80
            events = list(
                self.db.scalars(
                    select(ProviderUsageEvent)
                    .where(ProviderUsageEvent.tenant_id == ctx.tenant_id)
                    .execution_options(populate_existing=True)
                )
            )
            latest = {}
            for event in events:
                latest[event.id] = self.db.scalar(
                    select(UsageRevision)
                    .where(UsageRevision.event_id == event.id)
                    .order_by(UsageRevision.version.desc())
                    .limit(1)
                    .execution_options(populate_existing=True)
                )
            by_component = {
                (event.provider_connection_id, event.operation_key, event.component): event
                for event in events
            }
            # The latest distinct child facts constrain containment, never the billed
            # subtotal. Build this context before filtering so a window cannot hide
            # a contradictory sibling or turn a parent-only view into a complete total.
            siblings: dict[UUID, list[tuple[ProviderUsageEvent, UsageRevision]]] = {}
            for child in events:
                child_revision = latest[child.id]
                if child_revision is None or child_revision.cost_amount is None:
                    continue
                parent_component = child_revision.payload.get("included_in")
                if not isinstance(parent_component, str) or not parent_component:
                    continue
                parent = by_component.get(
                    (child.provider_connection_id, child.operation_key, parent_component)
                )
                parent_revision = latest.get(parent.id) if parent else None
                if (
                    parent is not None
                    and parent_revision is not None
                    and parent_revision.cost_amount is not None
                    and child_revision.payload.get("bundle")
                    and child_revision.payload.get("bundle") == parent_revision.payload.get("bundle")
                    and not parent_revision.payload.get("included_in")
                    and child_revision.currency == parent_revision.currency
                    and (child.operation, child.provider, child.provider_kind, child.synthetic)
                    == (parent.operation, parent.provider, parent.provider_kind, parent.synthetic)
                ):
                    siblings.setdefault(parent.id, []).append((child, child_revision))
            overfull: dict[UUID, list[tuple[ProviderUsageEvent, UsageRevision]]] = {}
            for parent_id, children in siblings.items():
                parent_revision = latest[parent_id]
                assert parent_revision is not None and parent_revision.cost_amount is not None
                known_children = Decimal(0)
                for _, child_revision in children:
                    assert child_revision.cost_amount is not None
                    known_children += child_revision.cost_amount
                if len(children) > 1 and known_children > parent_revision.cost_amount:
                    overfull[parent_id] = children
            overfull_child_ids = {child.id for children in overfull.values() for child, _ in children}
            for event in events:
                if not included(event.provider_connection_id, event.created_at):
                    continue
                revision = latest[event.id]
                if revision is None or revision.currency is None:
                    continue
                total = group(revision.currency, event.synthetic)
                provider_key = (event.provider_connection_id, revision.currency, event.synthetic)
                basis_row = providers.setdefault(
                    provider_key,
                    ProviderUsageBasis(
                        provider_connection_id=event.provider_connection_id,
                        provider=event.provider,
                        provider_kind=event.provider_kind,
                        currency=revision.currency,
                        synthetic=event.synthetic,
                        reported_amount="0",
                        estimated_amount="0",
                        unknown_count=0,
                        bundle_issue_count=0,
                        completeness="complete",
                    ),
                )
                parent_component = revision.payload.get("included_in")
                if isinstance(parent_component, str) and parent_component:
                    parent = by_component.get(
                        (event.provider_connection_id, event.operation_key, parent_component)
                    )
                    parent_revision = latest.get(parent.id) if parent else None
                    reason: BundleIssueReason | None = None
                    if parent is None or parent_revision is None:
                        reason = "PARENT_MISSING"
                    elif (
                        not revision.payload.get("bundle")
                        or parent_revision.payload.get("bundle") != revision.payload.get("bundle")
                        or parent_revision.payload.get("included_in")
                        or parent_revision.currency != revision.currency
                        or (parent.operation, parent.provider, parent.provider_kind, parent.synthetic)
                        != (event.operation, event.provider, event.provider_kind, event.synthetic)
                    ):
                        reason = "BUNDLE_IDENTITY_CONFLICT"
                    elif parent_revision.cost_amount is None:
                        reason = "PARENT_AMOUNT_UNKNOWN"
                    elif (
                        revision.cost_amount is not None
                        and revision.cost_amount > parent_revision.cost_amount
                    ):
                        reason = "COMPONENT_EXCEEDS_PARENT"
                    elif not included(parent.provider_connection_id, parent.created_at):
                        reason = "PARENT_OUTSIDE_FILTER"
                    if reason and event.id not in overfull_child_ids:
                        bundle_issues.append(
                            BundleIssue(
                                event_id=event.id,
                                parent_event_id=parent.id if parent else None,
                                provider_connection_id=event.provider_connection_id,
                                component=event.component,
                                parent_component=parent_component,
                                currency=revision.currency,
                                parent_currency=parent_revision.currency if parent_revision else None,
                                component_amount=format(revision.cost_amount, "f")
                                if revision.cost_amount is not None
                                else None,
                                parent_amount=format(parent_revision.cost_amount, "f")
                                if parent_revision and parent_revision.cost_amount is not None
                                else None,
                                reason=reason,
                            )
                        )
                        total["issues"] += 1
                        basis_row = basis_row.model_copy(
                            update={"bundle_issue_count": basis_row.bundle_issue_count + 1}
                        )
                        providers[provider_key] = basis_row
                        notices.add(
                            "Unresolved bundle overlap: total unavailable; "
                            "top-level known subtotal is partial. "
                            "Conflicting component facts are shown separately."
                        )
                if revision.cost_amount is None:
                    total["unknown"] += 1
                    providers[provider_key] = basis_row.model_copy(
                        update={"unknown_count": basis_row.unknown_count + 1}
                    )
                elif not revision.payload.get("included_in"):
                    total["known"] += revision.cost_amount
                    basis = "estimated" if revision.cost_quality == "RATE_ESTIMATE" else "reported"
                    total[basis] += revision.cost_amount
                    field = basis + "_amount"
                    providers[provider_key] = basis_row.model_copy(
                        update={field: format(Decimal(getattr(basis_row, field)) + revision.cost_amount, "f")}
                    )
            for parent_id, children in overfull.items():
                parent = next(event for event in events if event.id == parent_id)
                if not any(
                    included(item.provider_connection_id, item.created_at)
                    for item in [parent, *(child for child, _ in children)]
                ):
                    continue
                parent_revision = latest[parent_id]
                assert parent_revision is not None and parent_revision.cost_amount is not None
                for child, revision in children:
                    assert revision.currency is not None and revision.cost_amount is not None
                    bundle_issues.append(
                        BundleIssue(
                            event_id=child.id,
                            parent_event_id=parent.id,
                            provider_connection_id=child.provider_connection_id,
                            component=child.component,
                            parent_component=parent.component,
                            currency=revision.currency,
                            parent_currency=parent_revision.currency,
                            component_amount=format(revision.cost_amount, "f"),
                            parent_amount=format(parent_revision.cost_amount, "f"),
                            reason="BUNDLE_COMPONENTS_EXCEED_PARENT",
                            outside_filter=not included(child.provider_connection_id, child.created_at),
                        )
                    )
                    group(revision.currency, child.synthetic)["issues"] += 1
                    provider_key = (child.provider_connection_id, revision.currency, child.synthetic)
                    basis_row = providers[provider_key]
                    providers[provider_key] = basis_row.model_copy(
                        update={"bundle_issue_count": basis_row.bundle_issue_count + 1}
                    )
                notices.add(
                    "Unresolved bundle overlap: total unavailable; "
                    "top-level known subtotal is partial. Conflicting component facts are shown separately."
                )
            for reservation in self.db.scalars(
                select(BudgetReservation).where(BudgetReservation.tenant_id == ctx.tenant_id)
            ):
                if not included(reservation.provider_connection_id, reservation.created_at):
                    continue
                if reservation.status == "BLOCKED":
                    notices.add("Budget request blocked: " + reservation.reason)
                total = group(reservation.currency, True)
                total["reserved"] += reservation.retained_amount
                total["spent"] += reservation.spent_amount
                if reservation.status in {"UNKNOWN_SIDE_EFFECT", "ABANDONED"}:
                    notices.add("Unknown outcome retains reserved exposure; automatic retry is disabled.")
                if reservation.spent_amount > (reservation.original_amount or Decimal(0)):
                    notices.add("Actual cost exceeded its bound; further work is blocked at the cap.")
            currencies = [
                CurrencyUsage(
                    currency=currency,
                    synthetic=synthetic,
                    known_amount=format(value["known"], "f"),
                    reported_amount=format(value["reported"], "f"),
                    estimated_amount=format(value["estimated"], "f"),
                    unknown_count=int(value["unknown"]),
                    bundle_issue_count=int(value["issues"]),
                    completeness="partial" if value["unknown"] or value["issues"] else "complete",
                    reserved_amount=format(value["reserved"], "f"),
                    spent_amount=format(value["spent"], "f"),
                )
                for (currency, synthetic), value in sorted(groups.items())
            ]
        unresolved = list(
            self.db.scalars(
                select(UsageAttempt).where(
                    UsageAttempt.tenant_id == ctx.tenant_id, UsageAttempt.event_id.is_(None)
                )
            )
        )
        if unresolved:
            notices.add(
                f"Unresolved provider attempts: {len(unresolved)}; "
                "price/outcome unknown, never assumed free. "
                "Automatic retry is disabled."
            )
        pending_business = self.db.scalar(
            select(UsageAttempt.id)
            .where(
                UsageAttempt.tenant_id == ctx.tenant_id,
                UsageAttempt.event_id.is_not(None),
                UsageAttempt.business_outcome == "PENDING",
            )
            .limit(1)
        )
        if pending_business:
            notices.add("Business commit outcome unresolved; provider usage is retained independently.")
        limits = [
            LimitView(
                id=limit.id,
                dimension=cast(Dimension, scope.dimension),
                currency=limit.currency,
                amount=format(limit.amount, "f"),
                state=limit.state,
            )
            for limit, scope in self.db.execute(
                select(BudgetLimit, BudgetScope)
                .join(BudgetScope, BudgetScope.id == BudgetLimit.scope_id)
                .where(BudgetLimit.tenant_id == ctx.tenant_id)
            )
        ]
        return UsageSummary(
            currencies=currencies,
            providers=[
                row.model_copy(
                    update={
                        "completeness": "partial"
                        if row.unknown_count or row.bundle_issue_count
                        else "complete"
                    }
                )
                for row in providers.values()
            ],
            bundle_issues=bundle_issues,
            limits=limits,
            notices=sorted(notices),
        )
