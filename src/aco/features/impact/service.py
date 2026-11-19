"""Human Membership-scoped reporting over persisted facts; never invokes a provider."""

from datetime import UTC
from typing import Any, Literal

from fastapi import HTTPException
from sqlalchemy import Select, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from aco.core.clock import Clock, FrozenClock, SystemClock
from aco.core.context import TenantContext
from aco.features.accounting.models import InvoiceSnapshot, VerifiedPaymentAllocation
from aco.features.actions.models import ScheduledAction
from aco.features.agent.models import AgentRun
from aco.features.cases.models import CollectionCase
from aco.features.identity.models import ProviderConnection
from aco.features.impact.dto import ActivityGroup, ActivityRecord, ImpactQuery, ImpactView, UnavailableMetric
from aco.features.impact.financial_metrics import CashFact, InvoiceBasis, known_cash
from aco.features.receivables.queries import MetricQuery, ReceivableQueries, freshness
from aco.features.usage.dto import UsageFilters
from aco.features.usage.models import ProviderUsageEvent
from aco.features.usage.service import UsageService


class ImpactQueries:
    def __init__(self, db: AsyncSession, clock: Clock | None = None):
        self.db, self.clock = db, clock or SystemClock()

    async def read(self, ctx: TenantContext, query: ImpactQuery) -> ImpactView:
        if not isinstance(ctx, TenantContext):
            raise HTTPException(403, "HUMAN_CONTEXT_REQUIRED")
        await ReceivableQueries(self.db).tenant(ctx)
        at = self.clock.now().astimezone(UTC)
        since, until = query.since.astimezone(UTC), query.until.astimezone(UTC)
        if until > at:
            raise HTTPException(422, "REPORTING_WINDOW_FUTURE")
        operations = await ReceivableQueries(self.db, FrozenClock(at)).get_operations(ctx, MetricQuery())
        rows = list(
            await self.db.scalars(select(InvoiceSnapshot).where(InvoiceSnapshot.tenant_id == ctx.tenant_id))
        )
        connections = {
            row.id: row
            for row in await self.db.scalars(
                select(ProviderConnection).where(ProviderConnection.tenant_id == ctx.tenant_id)
            )
        }
        not_ready = {
            row.provider_connection_id
            for row in rows
            if row.provider_connection_id not in connections
            or connections[row.provider_connection_id].provider_type != "accounting"
            or connections[row.provider_connection_id].status != "READY"
        }
        source_warnings = [
            f"Accounting source {connection_id} is not ready. Imported balances and cash are "
            "last-known evidence; this report does not fetch or verify the provider."
            for connection_id in sorted(not_ready, key=str)
        ]
        invoices = {
            row.id: InvoiceBasis(
                row.id,
                row.tenant_id,
                row.provider_connection_id,
                row.organisation_id,
                row.currency,
                row.exponent,
                row.synthetic,
                "unknown" if row.provider_connection_id in not_ready else freshness(row, at),
                row.last_read_at,
            )
            for row in rows
        }
        allocations = await self.db.scalars(
            select(VerifiedPaymentAllocation).where(
                VerifiedPaymentAllocation.tenant_id == ctx.tenant_id,
                VerifiedPaymentAllocation.created_at <= at,
            )
        )
        facts = [
            CashFact(
                row.id,
                row.tenant_id,
                row.provider_connection_id,
                row.invoice_id,
                row.organisation_id,
                row.currency,
                row.exponent,
                row.synthetic,
                row.kind,
                row.minor_units,
                row.parent_id,
                row.effective_at,
                row.observed_at,
                row.created_at,
                row.source_revision,
            )
            for row in allocations
        ]
        usage = await self.db.run_sync(
            lambda store: UsageService(store).list_usage(ctx, UsageFilters(since=since, until=until))
        )
        activity: list[ActivityGroup] = []
        recent: list[ActivityRecord] = []

        async def summarize(
            kind: Literal["agent_runs", "scheduled_actions", "cases", "usage_events"],
            statement: Select[Any, Any, Any, Any, Any],
        ) -> None:
            # Aggregate all scoped records; only detail links are limited to the five newest.
            source = statement.subquery()
            counts = await self.db.execute(
                select(source.c.status, source.c.synthetic, func.count()).group_by(
                    source.c.status, source.c.synthetic
                )
            )
            by_provenance: dict[bool, dict[str, int]] = {}
            for status, synthetic, count in counts:
                by_provenance.setdefault(bool(synthetic), {})[str(status)] = int(count)
            for synthetic, statuses in by_provenance.items():
                activity.append(
                    ActivityGroup(
                        kind=kind, synthetic=synthetic, count=sum(statuses.values()), statuses=statuses
                    )
                )
            for row in await self.db.execute(statement.order_by(source_created(statement)).limit(5)):
                recent.append(
                    ActivityRecord(
                        kind=kind,
                        id=row.id,
                        invoice_id=row.invoice_id,
                        status=row.status,
                        synthetic=row.synthetic,
                        created_at=row.created_at,
                    )
                )

        def source_created(statement: Select[Any, Any, Any, Any, Any]) -> Any:
            return statement.selected_columns.created_at.desc()

        await summarize(
            "agent_runs",
            select(
                AgentRun.id,
                literal(None).label("invoice_id"),
                AgentRun.status,
                (AgentRun.authenticity == "FAKE").label("synthetic"),
                AgentRun.created_at,
            ).where(
                AgentRun.tenant_id == ctx.tenant_id, AgentRun.created_at >= since, AgentRun.created_at < until
            ),
        )
        await summarize(
            "scheduled_actions",
            select(
                ScheduledAction.id,
                ScheduledAction.invoice_id,
                ScheduledAction.status,
                InvoiceSnapshot.synthetic,
                ScheduledAction.created_at,
            )
            .join(
                InvoiceSnapshot,
                (InvoiceSnapshot.id == ScheduledAction.invoice_id)
                & (InvoiceSnapshot.tenant_id == ScheduledAction.tenant_id)
                & (InvoiceSnapshot.provider_connection_id == ScheduledAction.provider_connection_id),
            )
            .where(
                ScheduledAction.tenant_id == ctx.tenant_id,
                ScheduledAction.created_at >= since,
                ScheduledAction.created_at < until,
            ),
        )
        await summarize(
            "cases",
            select(
                CollectionCase.id,
                literal(None).label("invoice_id"),
                CollectionCase.status,
                CollectionCase.synthetic,
                CollectionCase.created_at,
            ).where(
                CollectionCase.tenant_id == ctx.tenant_id,
                CollectionCase.created_at >= since,
                CollectionCase.created_at < until,
            ),
        )
        await summarize(
            "usage_events",
            select(
                ProviderUsageEvent.id,
                literal(None).label("invoice_id"),
                literal("recorded").label("status"),
                ProviderUsageEvent.synthetic,
                ProviderUsageEvent.created_at,
            ).where(
                ProviderUsageEvent.tenant_id == ctx.tenant_id,
                ProviderUsageEvent.created_at >= since,
                ProviderUsageEvent.created_at < until,
            ),
        )
        return ImpactView(
            tenant_id=ctx.tenant_id,
            since=since,
            until=until,
            as_of_at=at,
            source_warnings=source_warnings,
            operations=operations,
            cash=known_cash(ctx.tenant_id, since, until, at, facts, invoices),
            activity=activity,
            recent_activity=sorted(recent, key=lambda row: row.created_at, reverse=True),
            usage=usage,
            unavailable=[
                UnavailableMetric(
                    metric="Causal impact from observed-cohort comparisons (BI-01/02/04)",
                    reason=(
                        "Frozen observed-cohort results and separate fixed illustrative synthetic cohorts "
                        "are shown in their own panels when source evidence supports them. Their source, "
                        "maturity and comparability limits remain explicit; neither establishes "
                        "causal impact."
                    ),
                ),
                UnavailableMetric(
                    metric="Measured human-time coverage and attribution of unbound approvals",
                    reason=(
                        "Case-bound decisions use exact immutable case/action links in the separate "
                        "case-history projection. Unbound historical approvals remain unattributed; event "
                        "counts and manual reported minutes do not establish measured handling "
                        "or labour savings."
                    ),
                ),
                UnavailableMetric(
                    metric="Achieved savings and measured ROI (BI-08/09)",
                    reason=(
                        "Operating economics shows canonical usage allocations and Owner-reviewed versioned "
                        "assumptions. Explicit gaps include missing provider billing, unallocated usage and "
                        "mixed currencies. Reported minutes are not measured handling; estimated ROI remains "
                        "unavailable without required measured handling and complete same-currency costs."
                    ),
                ),
            ],
        )
